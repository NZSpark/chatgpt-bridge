"""E2E 探测：dump ChatGPT 真实 DOM 中所有关键元素的结构。

目的：一次性校准全部选择器（输入框 / 发送按钮 / 回复节点 / 代码块 /
新建对话），而不是逐个踩坑。默认不运行：需 CHATGPT_E2E=1。

运行：

    pkill -f chatgpt_api_server.py
    CHATGPT_E2E=1 E2E_HEADED=1 .venv/bin/python -m unittest \
        tests.e2e.test_dom_probe -v
"""

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


# 枚举所有可能承载 composer 的元素，输出可判定的属性。
_COMPOSER_JS = r"""
() => {
  const out = [];
  const sel = 'textarea, [contenteditable="true"], [contenteditable=""],'
            + ' rich-textarea, [role="textbox"], form, [data-testid]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    out.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 120),
      role: el.getAttribute('role'),
      ariaLabel: el.getAttribute('aria-label'),
      placeholder: el.getAttribute('placeholder'),
      dataTestId: el.getAttribute('data-testid'),
      contentEditable: el.getAttribute('contenteditable'),
      visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden',
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      parentTag: el.parentElement ? el.parentElement.tagName.toLowerCase() : null,
      parentTestId: el.parentElement ? el.parentElement.getAttribute('data-testid') : null,
    });
  }
  return out;
}
"""

# 枚举表单里所有 button，用于定位发送按钮。
_BUTTONS_JS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('button, [role="button"]')) {
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      ariaLabel: el.getAttribute('aria-label'),
      dataTestId: el.getAttribute('data-testid'),
      type: el.getAttribute('type'),
      disabled: el.disabled === true,
      text: (el.innerText || el.textContent || '').trim().slice(0, 40),
      visible: r.width > 0 && r.height > 0,
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
    });
  }
  return out;
}
"""


def _report(name, items, predicate):
    matched = [it for it in items if predicate(it)]
    print(f"\n===== {name}（命中 {len(matched)} / 共 {len(items)}）=====")
    for it in matched:
        print(f"  {it}")
    return matched


@unittest.skipUnless(GATE, "需 CHATGPT_E2E=1（真实访问 ChatGPT）")
class DomProbe(unittest.TestCase):

    def test_probe_dom(self):
        if not PROFILE.exists():
            self.skipTest(f"缺少登录目录 {PROFILE}")

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
            page.wait_for_timeout(4000)

            title = page.title()
            n_buttons = page.evaluate(
                "() => document.querySelectorAll('button, a, [role=button]').length"
            )
            print(f"\n[页面] title={title!r}  可点击元素={n_buttons}")
            if "just a moment" in title.lower() or n_buttons == 0:
                print("!! Cloudflare 挑战页 / 空页面，探测无意义。请用有头模式。")
                self.skipTest("页面被 Cloudflare 拦截或无内容")

            # ---- 当前配置的选择器命中情况 ----
            print("\n===== 当前配置选择器命中 ===")
            for label, selectors in (
                ("INPUT_SELECTORS", config.INPUT_SELECTORS),
                ("SEND_BUTTON_SELECTORS", config.SEND_BUTTON_SELECTORS),
                ("READY_SELECTOR", [config.READY_SELECTOR]),
                ("NEW_CHAT_SELECTOR", config.NEW_CHAT_SELECTOR.split("||")),
            ):
                print(f"  -- {label} --")
                for sel in selectors:
                    sel = sel.strip()
                    if not sel:
                        continue
                    try:
                        n = len(page.query_selector_all(sel))
                    except Exception as exc:  # noqa: BLE001
                        n = f"ERR({exc!r})"
                    print(f"    {sel!r}: {n}")

            # ---- composer 候选 ----
            comp = page.evaluate(_COMPOSER_JS)
            _report(
                "可见 composer 候选（contenteditable/textarea/role=textbox）",
                comp,
                lambda it: it["visible"] and (
                    it["contentEditable"] == "true"
                    or it["tag"] == "textarea"
                    or it["role"] == "textbox"
                ),
            )

            # ---- 发送按钮候选 ----
            btns = page.evaluate(_BUTTONS_JS)
            _report(
                "可见按钮（带 aria-label/testid，可能是发送）",
                btns,
                lambda it: it["visible"] and (
                    (it["ariaLabel"] or "").lower().find("send") >= 0
                    or (it["dataTestId"] or "").find("send") >= 0
                    or it["type"] == "submit"
                ),
            )

            # ---- 实际在 composer 里输入，观察发送按钮出现 ----
            print("\n===== 输入探测文本后，composer 附近按钮变化 =====")
            target = None
            for it in comp:
                if it["visible"] and (
                    it["contentEditable"] == "true" or it["tag"] == "textarea"
                ):
                    target = it
                    break
            if target:
                print(f"  选中目标: {target}")
                try:
                    el = page.query_selector(
                        'div[contenteditable="true"], textarea'
                    )
                    el.click()
                    page.keyboard.type("ping")
                    page.wait_for_timeout(800)
                    btns2 = page.evaluate(_BUTTONS_JS)
                    _report(
                        "输入后可见按钮（可能是发送）",
                        btns2,
                        lambda it: it["visible"] and (
                            (it["ariaLabel"] or "").lower().find("send") >= 0
                            or (it["dataTestId"] or "").find("send") >= 0
                            or it["type"] == "submit"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  输入探测失败: {exc!r}")
            else:
                print("  没找到可见 composer，跳过输入探测。")
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
