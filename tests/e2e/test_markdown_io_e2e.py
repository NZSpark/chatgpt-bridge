"""端到端：Markdown IO 的**模型链路**（阶段 9）走真实 ChatGPT 桥接链路。

链路：本地 .md 文件 → render_view（带行号/围栏标注）→ bridge /v1/chat/completions
     → 模型返回 TOOL_CALL: edit_markdown → generate_edit 解析 → apply_edit
     → write_md（dry-run）→ 校验 diff 与围栏结构。

设计说明（2026-10-06 收敛）：本模块**只保留网络价值**的用例——「模型是否按约定
返回 edit_markdown」是单测无法覆盖的部分。以下效果已由 tests/test_markdown_io.py
的单测完全覆盖，因此不再在本模块重复（删除时逐条核对过）：

* 读取字节保真（`ReadMdTests` / `FenceScanTests`）；
* 锚点定位、围栏内不参与结构匹配（`LocateTests`）；
* 整块替换保留语言标签（`ApplyEditTests.test_edit_rescans_fences` 等）；
* `render_view` 围栏标注（`RenderViewTests`）。

运行（真实访问 ChatGPT，默认 skip）：

    CHATGPT_E2E=1 .venv/bin/python -m unittest tests.e2e.test_markdown_io_e2e -v

未设置 CHATGPT_E2E 时全部 skip，不影响常规套件、不发起网络请求。
"""

import os
import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple

from chatgpt_web import config
from chatgpt_web.markdown_io import (
    MarkdownError,
    apply_edit,
    generate_edit,
    read_md,
    verify,
    write_md,
)
from chatgpt_web.toolcalls import format_tool_retry_nudge

from .bridge import BridgeClient, BridgeServer

GATE = os.environ.get("CHATGPT_E2E") == "1"
PORT = int(os.environ.get("E2E_PORT") or config.PORT)
BASE_URL = f"http://127.0.0.1:{PORT}"
MODEL_ID = "chatgpt-chat"

SERVER = None
BRIDGE = None

# --- T4.3：软失败指标化 -----------------------------------------------------
# 「模型未按约定返回 edit_markdown」不再用 skipTest 掩盖：单次强化重试后仍不
# 达标 = **软失败**（用例 FAIL + 下面的 tearDownModule 汇总），只有 bridge/上游
# 不可用（超时、502/503/504 等）才走 skip 通道。
_SOFT_FAILURES: List[str] = []
_UPSTREAM_TYPES = {"timeout", "upstream_error", "context_length_exceeded"}


class UpstreamUnavailable(RuntimeError):
    """bridge/上游不可用（环境问题；重试后仍失败才 skip）。"""

SAMPLE_MD = (
    "# 项目说明\n"
    "\n"
    "这是引言。\n"
    "\n"
    "```json\n"
    '{"name": "demo", "version": "1.0"}\n'
    "```\n"
    "\n"
    "## 安装\n"
    "\n"
    "```bash\n"
    "pip install demo\n"
    "```\n"
    "\n"
    "## 用法\n"
    "\n"
    "运行 demo 即可。\n"
)


def _cleanup_module():
    """幂等回收；用 addModuleCleanup 注册，setUpModule 抛错时也会执行（T2.2）。"""
    global SERVER, BRIDGE
    if SERVER is not None:
        SERVER.stop()
        SERVER = None
    BRIDGE = None


def setUpModule():
    global SERVER, BRIDGE
    if not GATE:
        return
    unittest.addModuleCleanup(_cleanup_module)
    SERVER = BridgeServer(BASE_URL)
    SERVER.ensure_started()
    BRIDGE = BridgeClient(BASE_URL, config.SESSION_KEY_HEADER)


def tearDownModule():
    _cleanup_module()
    if not GATE:
        return
    if _SOFT_FAILURES:
        print(f"\n[E2E 软失败汇总] 模型未按约定返回 edit_markdown：{len(_SOFT_FAILURES)} 条")
        for item in _SOFT_FAILURES:
            print(f"  - {item}")
    else:
        print("\n[E2E 软失败汇总] 0 条：edit_markdown 用例中模型均按约定返回。")

@unittest.skipUnless(GATE, "设置 CHATGPT_E2E=1 才运行真实端到端测试")
class MarkdownIoE2ETests(unittest.TestCase):
    """真实模型链路。每个用例自建临时文件，互不影响。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "README.md"
        self.path.write_text(SAMPLE_MD, encoding="utf-8")
        self._last_reply = ""

    def tearDown(self):
        self.tmpdir.cleanup()

    def _llm(self, prompt: str) -> str:
        """把 render_view 的提示词送进 bridge，取回模型原始文本。"""
        status, body = BRIDGE.chat(
            messages=[{"role": "user", "content": prompt}],
            model=MODEL_ID,
            session="e2e-md",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "edit_markdown",
                        "description": "按行号区间替换 Markdown 内容",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "integer"},
                                "end": {"type": "integer"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["start", "end", "new_text"],
                        },
                    },
                }
            ],
        )
        if status in (502, 503, 504):
            raise UpstreamUnavailable(f"HTTP {status}: {str(body)[:200]}")
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict) and err.get("type") in _UPSTREAM_TYPES:
            raise UpstreamUnavailable(f"{err.get('type')}: {str(body)[:200]}")
        self.assertEqual(status, 200, f"bridge 返回非 200：{body}")
        self._last_reply = body["choices"][0]["message"]["content"]
        return self._last_reply

    def _edit_with_retry(self, doc, instruction: str) -> Tuple[int, int, str]:
        """``generate_edit`` + 单次强化纠偏（与 T1.1 共用同一条纠偏指令，T4.3）。

        * 模型未按约定返回 ``edit_markdown``（强化重试后仍不达标）→ **软失败**：
          记入 ``_SOFT_FAILURES`` 并 ``fail``，不再静默 SKIP；
        * bridge/上游不可用（强化重试后仍失败）→ 才 ``skipTest``（环境问题）。
        """
        first: BaseException
        try:
            return generate_edit(doc, instruction, self._llm)
        except (MarkdownError, UpstreamUnavailable) as exc:
            first = exc
        try:
            return generate_edit(
                doc, instruction + "\n\n" + format_tool_retry_nudge(), self._llm
            )
        except UpstreamUnavailable as exc:
            self.skipTest(f"bridge/上游不可用（强化重试后仍失败，环境问题）：{exc}")
        except MarkdownError as exc:
            _SOFT_FAILURES.append(f"{self._testMethodName}: {exc}")
            self.fail(
                "软失败：模型未按约定返回 edit_markdown（已用同一纠偏指令重试一次）\n"
                f"  首次失败：{first}\n  重试失败：{exc}\n"
                f"  模型最后一次回复开头：{self._last_reply[:300]!r}"
            )

    # --- 唯一的联网用例：模型返回 TOOL_CALL + dry-run（见模块 docstring）-----
    def test_e2e_model_edit_dry_run(self):
        doc = read_md(self.path)
        start, end, new_text = self._edit_with_retry(
            doc, "把『## 用法』这一节正文改成：用法已更新，见在线文档。"
        )

        edited = apply_edit(doc, start, end, new_text)
        diff = write_md(edited, dry_run=True)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE_MD)  # dry-run 不落盘
        self.assertTrue(diff.strip())
        self.assertEqual(len(edited.fences), len(doc.fences))
        self.assertEqual(verify(edited), [])
