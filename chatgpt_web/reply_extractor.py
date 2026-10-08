"""Reply extraction and response-file persistence helpers (PI-026)."""

import logging
import time
import uuid
from pathlib import Path
from typing import List

from . import config
from .completion.extractor import strip_code_noise as _strip_code_noise_impl

logger=logging.getLogger(__name__)


def _prune_output_dir(output_dir: str) -> None:
    """按 config 的保留策略清理落盘目录（0 = 不限，出错静默忽略）。"""
    max_files = config.OUTPUT_MAX_FILES
    max_age_days = config.OUTPUT_MAX_AGE_DAYS
    if not max_files and not max_age_days:
        return
    try:
        entries = [p for p in Path(output_dir).iterdir() if p.is_file()]
    except Exception:
        return
    now = time.time()
    for path in entries:
        try:
            if max_age_days and now - path.stat().st_mtime > max_age_days * 86400:
                path.unlink()
        except Exception:
            continue
    if max_files:
        try:
            remaining = sorted(
                (p for p in Path(output_dir).iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            for path in remaining[: max(0, len(remaining) - max_files)]:
                try:
                    path.unlink()
                except Exception:
                    continue
        except Exception:
            return


class ReplyExtractorMixin:
    """Reply text/code extraction and output persistence compatibility facade."""

    _ANIMATED_JS = (
        "(n) => !!n.querySelector('.animating, .pending, .revealing, .fade-in')"
    )

    _COMPLETE_TEXT_JS = """
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

    @staticmethod
    def _strip_code_noise(code_content: str, lang: str) -> str:
        """剥离 ChatGPT 代码块界面噪声，但保留首尾空白（PI-902 迁至 extractor）。"""
        return _strip_code_noise_impl(code_content, lang)

    async def _extract_code_blocks(self, element) -> List[dict]:
        """兼容 facade：代码块提取统一由 DOM adapter 负责。"""
        return await self.dom.extract_code_blocks(element)

    async def _complete_text(self, node) -> str:
        """兼容 facade：完整文本读取统一由 DOM adapter 负责。"""
        return await self.dom.complete_text(node)

    async def _has_pending_tokens(self, node) -> bool:
        """兼容 facade：pending-token 检测统一由 DOM adapter 负责。"""
        return await self.dom.has_pending_tokens(node)

    @staticmethod
    def save_extracted_files(raw_text: str, code_blocks: List[dict], output_dir: str) -> List[str]:
        """将提取的代码落地为对应格式的文件"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        _prune_output_dir(output_dir)
        saved = []

        ext_map = {
            "python": "py", "py": "py", "javascript": "js", "js": "js",
            "html": "html", "css": "css", "json": "json", "cpp": "cpp",
            "c": "c", "bash": "sh", "shell": "sh", "sql": "sql", "markdown": "md"
        }

        # 同一秒内的多个请求会拿到同样的 timestamp，必须再加一段随机后缀，
        # 否则 code_<ts>_1.py / response_<ts>.md 会互相覆盖（多任务并行后很常见）
        unique = uuid.uuid4().hex[:6]

        if code_blocks:
            for idx, block in enumerate(code_blocks, start=1):
                lang = block["lang"].lower().strip()
                code = block["code"]
                ext = ext_map.get(lang, "py" if "import " in code or "def " in code else "txt")

                timestamp = int(time.time())
                filename = f"code_{timestamp}_{idx}_{unique}.{ext}"
                filepath = Path(output_dir) / filename

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(code)
                saved.append(str(filepath))
                logger.info(f"[已保存文件] {filepath}")
        else:
            filename = f"response_{int(time.time())}_{unique}.md"
            filepath = Path(output_dir) / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_text)
            saved.append(str(filepath))

        return saved
