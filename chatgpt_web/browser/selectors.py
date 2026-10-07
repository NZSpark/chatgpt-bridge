"""DOM 选择器与 JS 片段集中管理（PI-903-2）。

原先散落在 :class:`chatgpt_web.dom_adapter.ChatGPTDOMAdapter` 类体里的 JS 片段与
硬编码 fallback 选择器集中到这里，作为**唯一来源**。业务代码不直接写 selector：

* 可被 ``.env`` 覆盖的候选选择器仍在 :mod:`chatgpt_web.config`（``INPUT_SELECTORS`` /
  ``RESPONSE_SELECTORS`` / ``NEW_CHAT_SELECTOR`` 等），由 adapter 读取——这也是测试
  用 ``patch.object(config, ...)`` 生效的契约；
* 纯代码内的 JS 片段（读文本、探测停止按钮、Think 兜底扫描等）与硬编码 fallback
  选择器放这里。

``ChatGPTDOMAdapter`` 仍把这些名字作为**类属性**暴露（值引用自本模块），
因此历史调用方 ``adapter.GENERATING_JS`` / 测试里读取 ``STOP_TOKEN_PATTERN`` 依旧生效。
"""

# 选择器版本号：网页版改版导致选择器失效时用于诊断（/_debug/selectors）。
SELECTOR_VERSION = "2026-10-07"

# 「停止生成」类名的判定口径：类名里必须是**独立**的 stop 词。
# 线上回归：侧边栏会话标题 class 带 ``stopAtEnd-<hash>``（截断样式），在视口下半部
# 可见，/stop/i 会把它误判成停止控件 → generating 恒为 True → 结束判定永远等不到
# 「页面落定」，每轮空转到超时。收紧为「stop 前后是分隔符或边界」。
STOP_TOKEN_PATTERN = r"(^|[-_])stop([-_]|$)"

# ---- JS 片段 ----

# Think 按钮兜底：配置选择器全部落空时，按文本扫描 composer 上的 pill。
THINK_FALLBACK_JS = """
    (want) => {
      const textOf = (el) => ((el.innerText || el.textContent || '').trim().toLowerCase());
      const boxed = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      const hits = [...document.querySelectorAll('button, [role="button"]')]
        .filter((el) => want.some((w) => textOf(el).includes(w)));
      return hits.find((el) => el.hasAttribute('aria-pressed') && boxed(el))
        || hits.find((el) => el.hasAttribute('aria-pressed'))
        || hits.find(boxed)
        || null;
    }
    """

# 读取回复节点完整文本：ChatGPT 把流式回复按 token 渲染成一串 <span class="animating">，
# Playwright 的 inner_text() 遵循**渲染后**可见性，动画未走完的 token 取不到——
# 表现为文本在引号/冒号处被截断（TOOL_CALL 的 JSON 参数被切掉半截）。
# 这里在克隆节点上移除动画类与 animation 样式，挂到屏幕外再读 innerText，
# 既拿到完整文本，又保留块级换行。
COMPLETE_TEXT_JS = """
    (node) => {
      const clone = node.cloneNode(true);
      clone.querySelectorAll('.animating, .pending, .revealing, .fade-in')
        .forEach(e => e.classList.remove('animating', 'pending', 'revealing', 'fade-in'));
      clone.querySelectorAll('[style]').forEach(e => {
        e.style.animation = 'none';
        e.style.opacity = '1';
        e.style.visibility = 'visible';
        e.style.filter = 'none';
        e.style.transform = 'none';
      });
      const holder = document.createElement('div');
      holder.style.position = 'absolute';
      holder.style.left = '-99999px';
      holder.style.top = '0';
      holder.appendChild(clone);
      document.body.appendChild(holder);
      const text = clone.innerText || clone.textContent || '';
      holder.remove();
      return text;
    }
    """

# 快速探测：节点内是否还有未显现的 token（动画类）。没有时 inner_text 已完整，
# 无需克隆节点（克隆 + 屏幕外挂载是明显的额外开销）。
ANIMATED_JS = "(n) => !!n.querySelector('.animating, .pending, .revealing, .fade-in')"

# 探测页面是否仍在生成：优先「停止生成」控件，且必须在视口下半部（composer 附近），
# 避免把顶部/侧边栏的无关按钮误判成停止控件。
GENERATING_JS = """
    () => {
      const stopRe = /STOP_TOKEN_PATTERN/i;
      const words = ['\\u505c\\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        '[data-testid*="stop"], button, [role="button"],'
        + ' div[class*="stop"], span[class*="stop"], svg[class*="stop"]'
      );
      for (const el of nodes) {
        const testid = el.getAttribute('data-testid') || '';
        const label = [
          testid,
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          (el.textContent || '').slice(0, 40),
        ].join(' ');
        const cls = typeof el.className === 'string' ? el.className : '';
        const byClass = stopRe.test(cls) || stopRe.test(testid);
        if (!words.some((w) => label.includes(w)) && !byClass) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0 && rect.top > window.innerHeight * 0.5) {
          return true;
        }
      }
      return false;
    }
    """.replace("STOP_TOKEN_PATTERN", STOP_TOKEN_PATTERN)

# 停止控件候选诊断：返回所有「可能像停止控件」的节点及其可见性/位置，
# 供 /_debug 与日志诊断（不暴露页面文本）。
STOP_CANDIDATES_JS = """
    () => {
      const stopRe = /STOP_TOKEN_PATTERN/i;
      const words = ['\\u505c\\u6b62', 'stop', 'Stop', 'STOP'];
      const nodes = document.querySelectorAll(
        '[data-testid*="stop"], button, [role="button"],'
        + ' div[class*="stop"], span[class*="stop"], svg[class*="stop"]'
      );
      return [...nodes].map((el) => {
        const testid = el.getAttribute('data-testid') || '';
        const label = [
          testid,
          el.getAttribute('aria-label') || '',
          el.getAttribute('title') || '',
          (el.textContent || '').slice(0, 40),
        ].join(' ');
        const cls = typeof el.className === 'string' ? el.className : '';
        const byClass = stopRe.test(cls) || stopRe.test(testid);
        const rect = el.getBoundingClientRect();
        const visible = rect.width > 0 && rect.height > 0;
        const bottomHalf = rect.top > window.innerHeight * 0.5;
        return { label, className: cls, visible, bottomHalf,
          matches: words.some((w) => label.includes(w)) || byClass };
      }).filter((x) => x.matches);
    }
    """.replace("STOP_TOKEN_PATTERN", STOP_TOKEN_PATTERN)

# 「会话到顶」探测：先把模型回复节点的文本从整页文本里剔除，避免把回复正文里
# 提到「长度上限」误判成网页版提示。%s 处注入 RESPONSE_SELECTORS 的 JSON。
CAP_CHECK_JS_TEMPLATE = (
    "() => { let text = document.body ? (document.body.innerText || '') : '';"
    " for (const node of document.querySelectorAll(%s)) {"
    " const t = node.innerText || ''; if (t) text = text.replace(t, ' '); }"
    " return text; }"
)

# 停止控件候选的硬编码 fallback 选择器（探测/诊断共用）。
STOP_CONTROL_SELECTOR = (
    '[data-testid*="stop"], button, [role="button"], '
    'div[class*="stop"], span[class*="stop"], svg[class*="stop"]'
)

__all__ = [
    "SELECTOR_VERSION",
    "STOP_TOKEN_PATTERN",
    "THINK_FALLBACK_JS",
    "COMPLETE_TEXT_JS",
    "ANIMATED_JS",
    "GENERATING_JS",
    "STOP_CANDIDATES_JS",
    "CAP_CHECK_JS_TEMPLATE",
    "STOP_CONTROL_SELECTOR",
]
