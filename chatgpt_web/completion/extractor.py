"""回复 / 代码块提取（PI-902 的 ``extractor`` 职责）。

负责：assistant reply 提取、code block 提取、tool call 检测。

拆分前这些逻辑一部分是 ``ChatIOMixin`` 里的兼容 facade（转发给 DOM adapter），
一部分是 :func:`_strip_code_noise` 这类纯函数。这里把**纯函数**集中过来，
DOM 相关的重活仍由 :mod:`chatgpt_web.dom_adapter` 承担（业务代码不直接写
selector，符合 ``doc/tasks_pi_9.md`` PI-903 的方向）。

行为与拆分前完全一致；``chatgpt_web.chat_io`` 继续作为兼容 facade 暴露
``ChatIOMixin._strip_code_noise`` / ``_extract_code_blocks``。
"""

import re


def strip_code_noise(code_content: str, lang: str) -> str:
    """剥离 ChatGPT 代码块界面噪声，但保留首尾空白。

    旧实现直接 .strip()，会无条件抹掉开头/结尾的空白与空行。
    对 README.md 这类要按原文匹配再改的文件是致命的：
    首行空行、末尾换行被删后，edit 工具逐字节匹配就会失败。

    这里只去掉头部的语言标签行与 Copy/Download 行，
    正文的空白、空行、首尾换行全部原样保留。
    """
    text = code_content.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    lang_alt = re.escape(lang) if lang else r"[A-Za-z0-9_+#.-]*"
    lang_line_re = re.compile(
        r"^\s*(?:" + lang_alt + r"|bash|shell|sh|python|py|json|html|javascript|js|"
        r"typescript|ts|css|sql|go|rust|java|cpp|c|markdown|md|txt)\s*$",
        re.IGNORECASE,
    )
    copy_line_re = re.compile(
        r"^\s*(?:" + lang_alt + r"|bash|shell|sh|python|py|json|html|javascript|js)?"
        r"\s*(?:Copy|Download)\s*$",
        re.IGNORECASE,
    )
    # 头部：允许先跳过空行，再剥语言标签 / Copy 行；
    # 一旦遇到第一行正文就停，避免误删正文里同名的行。
    start = 0
    while start < len(lines):
        line = lines[start]
        if not line.strip():
            start += 1
            continue
        if lang_line_re.match(line) or copy_line_re.match(line):
            start += 1
            continue
        break
    end = len(lines)
    while end > start:
        last = lines[end - 1]
        if last.strip() and copy_line_re.match(last):
            end -= 1
            continue
        break
    return "\n".join(lines[start:end])


__all__ = ["strip_code_noise"]
