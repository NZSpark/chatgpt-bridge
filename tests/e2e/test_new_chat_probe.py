"""E2E 探测：定位 ChatGPT 首页的「新建对话」按钮。

背景：``config.NEW_CHAT_SELECTOR`` 里写死的 aria-label 是旧版侧边栏的形态，
新版 ChatGPT 改版后经常全部落空，``_open_new_chat`` 于是静默跳过、沿用
当前会话页——表现为「找不到新建会话按钮」。

本用例不依赖任何既有选择器，而是真实打开首页，dump 所有可点击元素，
从 aria-label / title / data-testid / 文本 / 位置 多维度推断候选，并打印
一份可直接粘贴回 ``.env`` 的建议选择器。默认不运行：需 CHATGPT_E2E=1。

运行：

    CHATGPT_E2E=1 E2E_HEADED=1 .venv/bin/python -m unittest \
        tests.e2e.test_new_chat_probe -v
"""

import json
import os
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from chatgpt_web import config
from chatgpt_web.errors import HOME_URL

GATE = os.environ.get("CHATGPT_E2E") == "1"
E2E_HEADED = os.environ.get("E2E_HEADED") == "1"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE = (PROJECT_ROOT / config.USER_DATA_DIR).resolve()


# 在页面里枚举所有 <button> / <a> / [role=button]，收集可判定属性。
_DUMP_JS = r"""
() => {
  const out = [];
  const nodes = document.querySelectorAll('button, a, [role="button"]');
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      ariaLabel: el.getAttribute('aria-label'),
      title: el.getAttribute('title'),
      testid: el.getAttribute('data-testid'),
      text: (el.innerText || el.textContent || '').trim().slice(0, 60),
      visible: r.width > 0 && r.height > 0,
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
    });
  }
  return out;
}
"""


def _looks_like_new_chat(item: dict) -> bool:
    """按多语言关键词 + 位置启发式判断是否「新建对话」。"""
    hay = " ".join(
        str(item.get(k) or "")
        for k in ("ariaLabel", "title", "testid", "text")
    ).lower()
    keywords = (
        "new chat", "newchat", "new-chat",
        "新对话", "新聊天", "新建对话", "新建聊天",
        "start new", "compose",
    )
    return any(k in hay for k in keywords)


@unittest.skipUnless(GATE, "需 CHATGPT_E2E=1（真实访问 ChatGPT）")
class NewChatButtonProbe(unittest.TestCase):
    """真实首页 -> dump 按钮 -> 报告当前选择器是否命中。"""

    def test_probe_new_chat_button(self):
        if not PROFILE.exists():
            self.skipTest(f"缺少登录目录 {PROFILE}（先 HEADLESS=false 手动登录）")

        pw = sync_playwright().start()
        context = None
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE),
                headless=not E2E_HEADED,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            # 等页面基本就绪（侧边栏 / composer 出现）
            try:
                page.wait_for_selector(config.READY_SELECTOR, timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(2000)  # 给 React 侧边栏一点挂载时间

            buttons = page.evaluate(_DUMP_JS)
            visible = [b for b in buttons if b.get("visible")]

            print("\n===== 当前 NEW_CHAT_SELECTOR 命中情况 =====")
            for selector in config.NEW_CHAT_SELECTOR.split("||"):
                selector = selector.strip()
                if not selector:
                    continue
                try:
                    n = len(page.query_selector_all(selector))
                except Exception as exc:  # noqa: BLE001
                    n = f"ERR({exc})"
                print(f"  {selector!r}: 匹配 {n} 个")

            print(f"\n===== 可见可点击元素（{len(visible)} 个）=====")
            for b in visible[:80]:
                print(
                    f"  <{b['tag']}> aria-label={b['ariaLabel']!r} "
                    f"title={b['title']!r} testid={b['testid']!r} "
                    f"text={b['text']!r} @({b['x']},{b['y']}) {b['w']}x{b['h']}"
                )

            candidates = [b for b in visible if _looks_like_new_chat(b)]
            print("\n===== 推断的「新建对话」候选 =====")
            for c in candidates:
                print(f"  {json.dumps(c, ensure_ascii=False)}")

            # 输出建议选择器：优先 testid，其次 aria-label，再次文本
            print("\n===== 建议写入 .env 的 NEW_CHAT_SELECTOR =====")
            sels = []
            for c in candidates:
                if c.get("testid"):
                    sels.append(f'[data-testid="{c["testid"]}"]')
                if c.get("ariaLabel"):
                    sels.append(f'button[aria-label="{c["ariaLabel"]}"]')
            if sels:
                # 去重保序
                seen, uniq = set(), []
                for s in sels:
                    if s not in seen:
                        seen.add(s)
                        uniq.append(s)
                print("NEW_CHAT_SELECTOR=" + "||".join(uniq))
            else:
                print("（未从 aria/testid 推断出，需人工看上面 dump 的按钮）")

            self.assertTrue(
                visible,
                "首页没有任何可见按钮——页面可能没登录 / 被风控 / 结构大改",
            )
            print(
                f"\n[结论] 命中 {len(candidates)} 个候选；"
                f"当前配置选择器见上方「命中情况」。"
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
