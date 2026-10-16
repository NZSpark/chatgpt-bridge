"""OpenAI 兼容的数据模型。

参考: https://platform.openai.com/docs/api-reference/chat
Pi Coding Agent (pi.dev) 通过 models.json 里的 "api": "openai-completions"
接入任何 OpenAI 兼容端点，因此这里需要完整支持 messages / tools / stream。
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class FunctionCall(BaseModel):
    name: str
    # OpenAI 的 arguments 是 JSON 字符串；这里以字符串对外暴露
    arguments: str = "{}"


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:16]}")
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    # Pi 会发送 tool / tool_call_id / name 等字段，宽松接收避免校验失败
    model_config = ConfigDict(extra="allow")

    role: str
    # content 可能是字符串，也可能是内容分片数组
    # （如 [{"type": "text", "text": "hi"}]，Pi / 新版 OpenAI 客户端会这样发），
    # 因此用 Any 宽松接收，构建 prompt 时再归一化为纯文本。
    content: Optional[Any] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    # Pi 会附带大量标准字段（temperature、top_p、reasoning_effort、...）
    # 用 extra="allow" 全量吞掉，绝不因未知字段报 422
    model_config = ConfigDict(extra="allow")

    model: str = "chatgpt-chat"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    stream_options: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    # 网页版无法控制生成参数，以下字段仅作兼容占位（接收但不生效）
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[Any] = None
    # ---- 本地扩展字段（Pi 不会传，保持默认即可）----
    # None 表示「未显式指定」，落盘与否交给 config.SAVE_FILES 决定（默认 false）；
    # 只有客户端显式传 true/false 时才覆盖。避免默认产生落盘副作用。
    save_files: Optional[bool] = None
    output_dir: Optional[str] = None  # None -> 使用 .env 的 OUTPUT_DIR


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "chatgpt-chat"
    choices: List[Choice]
    usage: Usage = Field(default_factory=Usage)
    saved_files: List[str] = []


# 模型输入 / 输出模态声明（见 doc/image_upload.md §3.6）。
# 客户端（Pi 等）通过 GET /v1/models 探测模型能力；标准 OpenAI 响应只有
# ``{id, object, created, owned_by}``，不含模态字段，客户端据此默认 text-only，
# 会在请求构建阶段就把图片静默剥离——图片根本到不了桥接服务。
# 因此必须显式声明输入模态，且声明要诚实（关闭粘贴时仍声明 text+image，
# 因为此时图片会以 `[图片]` 文本引用形式进入模型，模型「看得到」这是张图）。
INPUT_MODALITIES = ["text", "image"]
OUTPUT_MODALITIES = ["text"]
MODALITY = "text+image->text"       # OpenRouter 风格模态串


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "chatgpt-web-bridge"
    # OpenAI 现行字段：显式声明输入 / 输出模态，客户端据此决定是否发送图片。
    input_modalities: List[str] = Field(default_factory=lambda: ["text", "image"])
    output_modalities: List[str] = Field(default_factory=lambda: ["text"])
    # OpenRouter 约定：部分客户端据此判定能否收图。两者都给，兼容面最广。
    architecture: Dict[str, Any] = Field(
        default_factory=lambda: {"modality": "text+image->text"}
    )


class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelCard]


# Pi 的 models.json 里引用的 id 需要与这里一致
SUPPORTED_MODELS = [
    {"id": "chatgpt-chat", "context_window": 65536},
    {"id": "chatgpt-reasoner", "context_window": 65536},
]


def advertised_models() -> List[Dict[str, Any]]:
    """本桥对外公布的模型清单。

    ``GET /v1/models`` 与聊天命令 ``/bridge models`` 共用这一份数据，避免两边
    各写一套清单后悄悄漂移（帮助文本里硬编码的模型名最容易过期）。
    """
    candidates = {m["id"]: m for m in SUPPORTED_MODELS}
    candidates.setdefault("chatgpt-chat", {"id": "chatgpt-chat"})
    return list(candidates.values())
