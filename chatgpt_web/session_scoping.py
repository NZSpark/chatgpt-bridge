"""Session bucket helpers for optional working-directory scoping."""

import hashlib
import ntpath
import posixpath
import re
from typing import Any, Iterable, Optional

_CWD_PATTERNS = (
    re.compile(r"<cwd>\s*(.*?)\s*</cwd>", re.IGNORECASE | re.DOTALL),
    re.compile(r"^[ \t]*Working Directory[ \t]*:[ \t]*(?P<path>[^\r\n]+?)[ \t]*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*Current Workspace Directory[ \t]*\([ \t]*(?P<path>[^)\r\n]+?)[ \t]*\)[ \t]*$", re.IGNORECASE | re.MULTILINE),
)
_MAX_CWD_LENGTH = 4096


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _content_text(content: Any) -> str:
    """Extract textual content without stringifying arbitrary multimodal data."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces)
    return ""


def cwd_candidate_texts(request: Any) -> list[str]:
    """Collect stable environment-bearing fields; never scan later user turns."""
    result: list[str] = []
    instructions = _field(request, "instructions")
    if isinstance(instructions, str) and instructions.strip():
        result.append(instructions)

    messages = _field(request, "messages")
    if isinstance(messages, list):
        for message in messages:
            if _field(message, "role") in ("system", "developer"):
                text = _content_text(_field(message, "content"))
                if text:
                    result.append(text)
        for message in messages:
            if _field(message, "role") == "user":
                text = _content_text(_field(message, "content"))
                if text:
                    result.append(text)
                break
        return result

    # Responses API uses input rather than messages.
    raw_input = _field(request, "input")
    if isinstance(raw_input, str):
        if raw_input:
            result.append(raw_input)
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                if item:
                    result.append(item)
                break
            if isinstance(item, dict) and (item.get("type") == "message" or ("role" in item and "content" in item)):
                if item.get("role", "user") == "user":
                    text = _content_text(item.get("content"))
                    if text:
                        result.append(text)
                    break
    return result


def _clean_path_candidate(value: str) -> Optional[str]:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"', "`"):
        value = value[1:-1].strip()
    value = value.rstrip(".,;!?").strip()
    if not value or len(value) > _MAX_CWD_LENGTH:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def normalize_cwd_path(value: str) -> Optional[str]:
    """Normalize an absolute POSIX/Windows path; reject relative paths."""
    if not isinstance(value, str):
        return None
    value = _clean_path_candidate(value)
    if value is None:
        return None
    drive, tail = ntpath.splitdrive(value)
    if drive and tail.startswith(("/", "\\")):
        return ntpath.normcase(ntpath.normpath(value)).replace("\\", "/")
    if value.startswith("/"):
        return posixpath.normpath(value)
    return None


def extract_cwd_path(candidate_texts: Iterable[str]) -> Optional[str]:
    """Return the first valid normalized cwd in deterministic source order."""
    for text in candidate_texts:
        if not isinstance(text, str) or not text:
            continue
        for pattern in _CWD_PATTERNS:
            for match in pattern.finditer(text):
                candidate = match.group("path") if "path" in match.groupdict() else match.group(1)
                normalized = normalize_cwd_path(candidate)
                if normalized:
                    return normalized
    return None


def cwd_bucket_key(request: Any, hash_len: int = 12, max_key_len: int = 64) -> Optional[str]:
    """Create a bounded stable key; the original cwd is never stored in it."""
    normalized = extract_cwd_path(cwd_candidate_texts(request))
    if not normalized:
        return None
    try:
        limit = int(max_key_len)
        requested_hash_len = int(hash_len)
    except (TypeError, ValueError):
        return None
    if limit < 5:
        return None
    digest_len = max(1, min(requested_hash_len, 64, limit - len("cwd:")))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:digest_len]
    basename = posixpath.basename(normalized.rstrip("/")) or "root"
    slug = re.sub(r"[^a-z0-9_-]+", "_", basename.lower()).strip("_-_") or "root"
    slug_room = limit - len("cwd:") - digest_len - 1
    key = f"cwd:{slug[:min(16, slug_room)]}-{digest}" if slug_room > 0 else f"cwd:{digest}"
    return key[:limit]
