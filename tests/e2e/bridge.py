"""ChatGPTBridge 的 HTTP 客户端与服务生命周期（标准库实现，无额外依赖）。

* :class:`BridgeServer` —— `/healthz` 探活；不通则以子进程拉起 uvicorn，
  并在 tearDown 时**只清理自己拉起的进程**（复用外部服务时不杀）。
* :class:`BridgeClient` —— OpenAI 兼容端点封装：非流式 / chat SSE / Responses SSE，
  统一带 `X-ChatGPT-Session` 分桶头。

端口：取 ``E2E_PORT``，未设置时回退 ``config.PORT``（仓库 ``.env`` 已定为 8002）。
判定矩阵与前置条件见 doc/e2e_test_design.md。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT_S = 90.0  # 浏览器冷启动可能要十几秒
# 兜底回收时用来识别 bridge 子进程的命令行特征
_BRIDGE_CMDLINE_MARKERS = ("uvicorn", "chatgpt_api_server")


def clear_profile_locks(profile: Path) -> List[str]:
    """删除 profile 里残留的 Chromium 单实例锁（异常退出后常见）。返回删除的文件名。"""
    removed: List[str] = []
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = profile / name
        try:
            path.unlink()
            removed.append(name)
        except OSError:
            pass
    return removed


def _bridge_pids() -> List[int]:
    """``pgrep`` 出所有 bridge/uvicorn 进程（仅供兜底回收使用）。"""
    try:
        out = subprocess.run(
            ["pgrep", "-fl", "uvicorn chatgpt_api_server"],
            capture_output=True, text=True, check=False,
        ).stdout
    except OSError:
        return []
    pids: List[int] = []
    for line in out.splitlines():
        token = line.strip().split(None, 1)[0] if line.strip() else ""
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def is_orphan(pid: int) -> bool:
    """该进程是否已被 init/launchd 收养（父进程已退出）。查询失败时保守返回 False。"""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        return int(out.split()[0]) <= 1
    except (OSError, ValueError, IndexError):
        return False


def reap_orphan_bridges(port: Optional[int] = None, *, log=print) -> List[int]:
    """兜底回收上一次失败运行残留的 bridge 子进程。

    只回收**孤儿**进程（父进程已退出，即真正被泄漏的那些），避免误杀用户
    自己在跑的 bridge；给了 ``port`` 时还会要求进程命令行匹配该端口。
    """
    marker = f"--port {port}" if port else None
    killed: List[int] = []
    for pid in _bridge_pids():
        if not is_orphan(pid):
            continue
        if marker is not None:
            try:
                cmdline = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    capture_output=True, text=True, check=False,
                ).stdout
            except OSError:
                continue
            if marker not in cmdline:
                continue
        if _terminate_pid(pid):
            killed.append(pid)
    if killed:
        log(f"[E2E] 已兜底回收残留的 bridge 进程：{killed}")
    return killed


def _terminate_pid(pid: int) -> bool:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return False
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                return True  # 进程已退出
    return True


def wait_port_closed(host: str, port: int, timeout_s: float = 10.0) -> bool:
    """等待端口不再接受连接（回收子进程后调用）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) != 0:
                return True
        time.sleep(0.2)
    return False


class BridgeServer:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.proc: Optional[subprocess.Popen] = None
        self.started_by_us = False
        self.log_path = Path(tempfile.gettempdir()) / "chatgpt_e2e_uvicorn.log"
        self._log_file = None

    # ---------- 进程生命周期 ----------

    def _close_log(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_file = None

    def _terminate_proc(self) -> None:
        """terminate → wait → kill，并确保句柄与日志文件被释放。"""
        proc, self.proc = self.proc, None
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=15)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                        proc.wait(timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
        self._close_log()

    def stop(self) -> None:
        """回收本测试拉起的 bridge（外部已有的服务不动）。"""
        if self.started_by_us:
            self._terminate_proc()
            host = self._host()
            port = self._port()
            if not wait_port_closed(host, port, timeout_s=10.0):
                print(f"[E2E] 警告：停止后端口 {host}:{port} 仍在监听")
        else:
            self._close_log()
        self.started_by_us = False

    # ---------- 解析 / 探活 ----------

    def _host(self) -> str:
        return self.base_url.split("://", 1)[-1].rsplit(":", 1)[0] or "127.0.0.1"

    def _port(self) -> int:
        return int(self.base_url.rsplit(":", 1)[-1])

    def healthz(self, timeout: float = 3.0) -> Optional[Tuple[int, Dict[str, Any]]]:
        """返回 (status, body)；服务不可达返回 None。"""
        try:
            req = urllib.request.Request(self.base_url + "/healthz", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                return exc.code, {}
        except Exception:  # noqa: BLE001
            return None

    def ensure_started(self) -> bool:
        """确保服务可用。返回 True 表示由本测试拉起（tearDown 需要清理）。

        任何失败路径都会先回收自己拉起的子进程再抛错：``unittest`` 在
        ``setUpModule`` 抛错时**不会调用** ``tearDownModule``（见 T2.2），
        残留的 uvicorn + Chromium 会独占 ``user_data``，让后续所有用例假失败。
        """
        probe = self.healthz(timeout=2.0)
        if probe is not None:
            self.started_by_us = False
            return False

        host = self._host()
        port = self._port()
        # 上一轮失败运行可能留下孤儿 uvicorn（ppid=1）占着 user_data 与端口
        reap_orphan_bridges(port)
        self._log_file = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "chatgpt_api_server:app",
                "--host", host,
                "--port", str(port),
            ],
            cwd=str(PROJECT_ROOT),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
        )
        self.started_by_us = True

        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            assert self.proc is not None
            if self.proc.poll() is not None:
                code = self.proc.returncode
                self._terminate_proc()
                self.started_by_us = False
                raise RuntimeError(
                    f"uvicorn 启动即退出（exit={code}），日志：{self.log_path}"
                )
            probe = self.healthz(timeout=2.0)
            if probe and probe[0] == 200:
                return True
            time.sleep(1.0)
        self._terminate_proc()
        self.started_by_us = False
        wait_port_closed(host, port, timeout_s=10.0)
        raise RuntimeError(
            f"bridge {STARTUP_TIMEOUT_S:.0f}s 内未就绪，日志：{self.log_path}"
        )


class BridgeClient:
    def __init__(self, base_url: str, session_header: str, timeout_s: float = 300.0):
        self.base_url = base_url
        self.session_header = session_header
        self.timeout_s = timeout_s

    # ---------- 底层 ----------

    def _headers(self, session: Optional[str]) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if session:
            headers[self.session_header] = session
        return headers

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        session: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers=self._headers(session),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as resp:
                body = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            status = exc.code
        try:
            return status, json.loads(body)
        except Exception:  # noqa: BLE001
            return status, body

    def _open_stream(self, path: str, payload: Dict[str, Any], session: Optional[str]):
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=self._headers(session),
        )
        return urllib.request.urlopen(req, timeout=self.timeout_s)

    # ---------- 端点 ----------

    def healthz(self) -> Tuple[int, Any]:
        return self.request("GET", "/healthz")

    def models(self) -> Tuple[int, Any]:
        return self.request("GET", "/v1/models")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        session: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        model: str = "chatgpt-chat",
        **extra: Any,
    ) -> Tuple[int, Any]:
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        payload.update(extra)
        return self.request("POST", "/v1/chat/completions", payload, session=session)

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        *,
        session: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        model: str = "chatgpt-chat",
        **extra: Any,
    ) -> Dict[str, Any]:
        """解析 chat SSE：返回 chunks / 拼接文本 / keep-alive 计时等。"""
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        payload.update(extra)

        result: Dict[str, Any] = {
            "status": 0, "chunks": [], "text": "", "keepalives": 0,
            "first_data_s": None, "elapsed_s": 0.0, "raw_text": "", "done": False,
        }
        started = time.monotonic()
        try:
            resp = self._open_stream("/v1/chat/completions", payload, session)
        except urllib.error.HTTPError as exc:
            result["status"] = exc.code
            result["raw_text"] = exc.read().decode("utf-8")
            result["elapsed_s"] = time.monotonic() - started
            return result

        with resp:
            result["status"] = resp.status
            pieces: List[str] = []
            raw_lines: List[str] = []
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                raw_lines.append(line)
                if not line:
                    continue
                if line.startswith(":"):
                    result["keepalives"] += 1
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    result["done"] = True
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:  # noqa: BLE001
                    continue
                if result["first_data_s"] is None:
                    result["first_data_s"] = time.monotonic() - started
                result["chunks"].append(chunk)
                for choice in chunk.get("choices") or []:
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        pieces.append(content)
            result["text"] = "".join(pieces)
            result["raw_text"] = "\n".join(raw_lines)
        result["elapsed_s"] = time.monotonic() - started
        return result

    def responses_stream(
        self,
        input_text: str,
        *,
        session: Optional[str] = None,
        model: str = "chatgpt-chat",
        **extra: Any,
    ) -> Dict[str, Any]:
        """解析 Responses 命名 SSE：返回事件名序列与数据载荷。"""
        payload: Dict[str, Any] = {"model": model, "input": input_text, "stream": True}
        payload.update(extra)
        result: Dict[str, Any] = {
            "status": 0, "events": [], "names": [], "raw_text": "",
            "first_event_s": None, "elapsed_s": 0.0,
        }
        started = time.monotonic()
        try:
            resp = self._open_stream("/v1/responses", payload, session)
        except urllib.error.HTTPError as exc:
            result["status"] = exc.code
            result["raw_text"] = exc.read().decode("utf-8")
            result["elapsed_s"] = time.monotonic() - started
            return result

        with resp:
            result["status"] = resp.status
            current_event: Optional[str] = None
            raw_lines: List[str] = []
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                raw_lines.append(line)
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except Exception:  # noqa: BLE001
                        current_event = None
                        continue
                    name = current_event or data.get("type") or "?"
                    if result["first_event_s"] is None:
                        result["first_event_s"] = time.monotonic() - started
                    result["names"].append(name)
                    result["events"].append({"name": name, "data": data})
                    current_event = None
            result["raw_text"] = "\n".join(raw_lines)
        result["elapsed_s"] = time.monotonic() - started
        return result

    def reset_session(self, session: str) -> Tuple[int, Any]:
        return self.request("POST", f"/session/reset?session={session}", None)
