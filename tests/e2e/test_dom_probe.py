"""E2E 探测：dump ChatGPT 真实 DOM 中所有关键元素的结构。

目的：一次性校准全部选择器（输入框 / 发送按钮 / 回复节点 / 代码块 /
新建对话），而不是逐个踩坑。默认不运行：需 CHATGPT_E2E=1。

2026-10-06 合并：原 `test_new_chat_probe.py` 的「新建对话候选推断 + 建议选择器」
已并入本模块——两者都在首页 dump 按钮、都打印 `NEW_CHAT_SELECTOR` 命中数，
拆分只会多启动一次浏览器；现在同一次页面加载里完成全部校准。

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
  for (const el of document.querySelectorAll('button, a, [role="button"]')) {
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


# 回复 / 代码块容器候选的命中情况（2026-10-06 网页版改版后新增）。
# 实测：助手回复已不在 [data-message-author-role] 上，而在
# [data-markdown-text-style] / [class*="MarkdownRoot"]；代码块换成
# [class*="CodeBlock"] + [data-language]（不再有 pre/code）。
_RESPONSE_JS = r"""
() => {
  const sels = [
    '[data-message-author-role="assistant"]',
    '[data-markdown-text-style]',
    '[class*="MarkdownRoot"]',
    'message-content',
    '.markdown',
    '[data-chatgpt-search-unit-key]',
    '[data-user-message-bubble]',
    '[class*="CodeBlock"]',
    '[data-language]',
    'pre',
    'code',
  ];
  const out = [];
  for (const sel of sels) {
    let els = [];
    try { els = [...document.querySelectorAll(sel)]; } catch (e) { els = []; }
    const last = els.length ? els[els.length - 1] : null;
    out.push({
      selector: sel,
      hits: els.length,
      sample: last ? {
        tag: last.tagName,
        cls: ((last.className || '') + '').slice(0, 80),
        textLen: (last.innerText || '').length,
      } : null,
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


def _looks_like_new_chat(item: dict) -> bool:
    """按多语言关键词判断是否「新建对话」候选（自 test_new_chat_probe 合并）。"""
    hay = " ".join(
        str(item.get(k) or "")
        for k in ("ariaLabel", "dataTestId", "text")
    ).lower()
    keywords = (
        "new chat", "newchat", "new-chat",
        "新对话", "新聊天", "新建对话", "新建聊天",
        "start new", "compose",
    )
    return any(k in hay for k in keywords)


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

            # ---- 「新建对话」候选 + 建议选择器（自 test_new_chat_probe 合并）----
            new_chat = _report(
                "可能是「新建对话」的按钮 / 链接",
                btns,
                lambda it: it["visible"] and _looks_like_new_chat(it),
            )
            print("\n===== 建议写入 .env 的 NEW_CHAT_SELECTOR =====")
            sels = []
            for c in new_chat:
                testid = c.get("dataTestId")
                aria = c.get("ariaLabel")
                tag = c.get("tag") or "button"
                if testid:
                    sels.append(f'[data-testid="{testid}"]')
                if aria:
                    sels.append(f'{tag}[aria-label="{aria}"]')
            if sels:
                # 去重保序
                sels = list(dict.fromkeys(sels))
                print("NEW_CHAT_SELECTOR=" + "||".join(sels))
            else:
                print("（未从 aria/testid 推断出，需人工看上面 dump 的元素）")

            # ---- 实际在 composer 里输入，观察发送按钮出现 ----
            # 关键：不要用 ElementHandle.click()——composer 常被 Playwright 判定为
            # not visible，click 会等 30s 后超时，探测在最关键的一步直接中断
            # （历史 bug，见 doc/tasks.md T3.4）。正确做法与 bridge 一致：
            # state="attached" 定位 + JS focus + keyboard.insert_text。
            print("\n===== 输入探测文本后，composer 附近按钮变化 =====")
            handle = None
            used_selector = None
            for selector in config.INPUT_SELECTORS:
                try:
                    candidate = page.query_selector(selector)
                except Exception:  # noqa: BLE001
                    candidate = None
                if candidate is not None:
                    handle, used_selector = candidate, selector
                    break
            if handle is None:
                print("  没找到 composer（attached 方式），跳过输入探测。")
            else:
                print(f"  使用输入框选择器: {used_selector!r}")
                try:
                    page.evaluate("(el) => { el.focus(); }", handle)
                    page.keyboard.insert_text("ping")
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
                    # 候选发送选择器的真实命中数（校准 SEND_BUTTON_SELECTORS）
                    print("  -- 发送按钮候选选择器命中数（输入后）--")
                    candidates = list(config.SEND_BUTTON_SELECTORS) + [
                        'button[data-testid="send-button"]',
                        'button[data-testid="composer-submit-button"]',
                        'button[aria-label*="Send"]',
                        'button[type="submit"]',
                    ]
                    for sel in dict.fromkeys(candidates):
                        sel = sel.strip()
                        if not sel:
                            continue
                        try:
                            n = len(page.query_selector_all(sel))
                        except Exception as exc:  # noqa: BLE001
                            n = f"ERR({exc!r})"
                        print(f"    {sel!r}: {n}")
                    # 清掉探测文本，别把草稿留给下一轮真实请求
                    page.evaluate(
                        """(el) => {
                          el.focus();
                          if (el.isContentEditable) { el.innerHTML = ''; }
                          else { el.value = ''; }
                          el.dispatchEvent(new InputEvent('input', {
                            bubbles: true, cancelable: true,
                            inputType: 'deleteContentBackward',
                          }));
                        }""",
                        handle,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  输入探测失败: {exc!r}")

            # ---- 回复 / 代码块容器校准（2026-10-06 改版：必须在一个**有历史**的
            #      会话上才有命中，因此这里打开侧边栏最近一条会话再 dump）。----
            print("\n===== 回复 / 代码块容器选择器命中（打开最近一条会话）=====")
            try:
                links = page.evaluate(
                    "() => [...document.querySelectorAll('a[href^=\"/c/\"]')]"
                    ".map((a) => a.href).slice(0, 1)"
                )
                if not links:
                    print("  侧边栏没有会话链接，跳过（先在网页里问一句再复跑）。")
                else:
                    page.goto(links[0], wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                    print(f"  会话页: {links[0]}")
                    print("  -- 当前配置 RESPONSE_SELECTORS 逐条命中数 --")
                    for sel in [s.strip() for s in config.RESPONSE_SELECTORS.split(",") if s.strip()]:
                        try:
                            count = len(page.query_selector_all(sel))
                        except Exception as exc:  # noqa: BLE001
                            count = f"ERR({exc!r})"
                        print(f"    {sel!r}: {count}")
                    _report(
                        "回复 / 代码块容器候选（末节点样本）",
                        page.evaluate(_RESPONSE_JS),
                        lambda it: True,
                    )
                    print("\n  判读：助手正文应命中 [data-markdown-text-style]（且 textLen>0）；"
                          "若全为 0 则网页版已改版，需按这里的 tag/class 重新校准。")
            except Exception as exc:  # noqa: BLE001
                print(f"  回复节点探测失败: {exc!r}")
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
