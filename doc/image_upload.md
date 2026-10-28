# ChatGPTBridge 图片上传支持设计与实现说明

> 本文以 ChatGPTBridge 当前代码为准，参考 DeepseekBridge 的网页图片直传方案，记录图片请求的解析、网页附件粘贴、协议接入、配置、失败降级、测试与安全边界。本文同时区分已实现行为与建议补充的加固措施，避免将计划中的能力误认为已落地。

## 1. 背景与目标

### 1.1 问题

ChatGPTBridge 将 OpenAI 兼容 API 请求转换为网页版 ChatGPT 的输入。纯文本消息可以直接写入网页 composer，但多模态请求中的图片不能仅靠把 `image_url` 转换成普通文本来完成上传。

例如，以下内容：

```json
{
  "type": "image_url",
  "image_url": {
    "url": "data:image/png;base64,..."
  }
}
```

如果只转换成 `[图片] (image/png, ~442KB base64)` 一类文字，网页并没有收到图片文件，模型也就无法通过该附件读取图像像素。

### 1.2 目标

1. 兼容 OpenAI 风格的图片分片，包括 base64 Data URI 和远程图片 URL。
2. 将有效图片转换为真实 `File`，通过页面内合成 `paste` 事件附加到网页版输入框。
3. 确保文本先进入 composer、图片随后附加、最后才提交，避免文本与附件分属不同消息。
4. 避免多轮请求反复粘贴历史图片。
5. 图片功能关闭、下载失败或粘贴失败时，保留文本请求并优雅降级。
6. 通过开关、每轮数量上限和下载超时控制行为。

### 1.3 设计边界

- 目标页面的主要输入控件是 `contenteditable`，不是普通文件上传框，不能简单依赖 Playwright 的 `set_input_files()`。
- 不要求使用宿主机的系统剪贴板；通过页面 JavaScript 构造 `File`、`DataTransfer` 和 `ClipboardEvent`。
- 文本 prompt 保留图片引用作为兜底，但引用文本不携带完整 base64。
- 本功能不等于直接调用 ChatGPT 的官方多模态 API；图片最终能否作为附件被网页编辑器接受，仍取决于当前网页 DOM、编辑器事件处理逻辑和登录状态。

## 2. 总体架构

处理分为五个阶段：**解析与引用 → 图片抽取 → 请求编排 → 浏览器粘贴 → 提交与降级**。

```text
OpenAI 兼容请求
       |
       +--> prompting._content_to_text()
       |       +--> 文本分片转换为文本
       |       +--> 图片分片转换为 [图片] 引用行
       |           不把完整 base64 放进 prompt
       |
       +--> extract_images(messages, only_delta=True)
               +--> 解析 Data URI 为原始字节
               +--> 远程 URL 暂存为待下载地址
                        |
                        v
             collect_images() / API 编排层
               +--> 检查 IMAGE_PASTE_ENABLED
               +--> 按 IMAGE_PASTE_MAX 截断
                        |
                        v
                 driver.send_chat(..., images=...)
                        |
                        v
               填充网页 composer 的文本
                        |
                        v
               _paste_images(page, composer, images)
                 +--> 获取远程图片字节（需要时）
                 +--> 确保 composer 聚焦
                 +--> 构造 File 和 DataTransfer
                 +--> 派发合成 paste 事件
                        |
                        v
                   提交同一条消息
                        |
                        v
                  轮询并提取模型回复
```

关键实现位置：

| 文件 | 职责 |
|---|---|
| `chatgpt_web/prompting.py` | `ImageAttachment`、图片类型识别、URL 提取、Data URI 解码、增量消息筛选、图片文本引用。 |
| `chatgpt_web/api/chat_adapter.py` | Chat Completions 的图片收集与上游调用编排，包括非流式、流式及部分本地续轮路径。 |
| `chatgpt_web/api/responses_adapter.py` | Responses 风格输入中的图片分片识别与原始内容保留。 |
| `chatgpt_web/chat_io.py` | 远程下载、聚焦 composer、合成粘贴事件，以及将图片参数向发送流程传递。 |
| `chatgpt_web/reply_waiter.py` | 在文本填入后、提交之前调用图片粘贴。 |
| `chatgpt_web/config/_core.py` | 图片上传配置默认值和范围约束。 |
| `chatgpt_web/models.py` | 对外模型元数据中的输入模态声明。 |
| `.env.example` | 用户可配置选项和默认示例。 |

## 3. API 输入与数据模型

### 3.1 支持的图片分片形式

`prompting._is_image_part()` 和 `prompting._image_url()` 当前兼容以下形式：

**标准 Chat Completions 形式：**

```json
{
  "type": "image_url",
  "image_url": {
    "url": "data:image/png;base64,..."
  }
}
```

**简化的 `image_url` 字符串形式：**

```json
{
  "type": "image_url",
  "image_url": "https://example.com/photo.jpg"
}
```

**Responses 风格形式：**

```json
{
  "type": "input_image",
  "image_url": "https://example.com/photo.jpg"
}
```

还兼容部分客户端使用的裸 `url` 或 `image` 字段。最终是否能得到可用的图片地址，取决于分片能否被 `_image_url()` 识别。

图片提取遍历消息的 `content` 数组。如果图片分片埋在不受支持的数据结构中，或者消息内容并不是数组，当前抽取函数不会自动将其转为附件。

### 3.2 图片附件对象

`prompting.ImageAttachment` 是图片提取层与浏览器层之间的内部数据对象：

```python
@dataclass(slots=True)
class ImageAttachment:
    name: str
    mime: str
    data: Optional[bytes] = None
    url: Optional[str] = None
```

- `name`：传递给页面 `File` 的文件名。
- `mime`：传递给页面 `File` 的 MIME 类型。
- `data`：已经解码的二进制内容。
- `url`：远程图片地址，稍后由浏览器上下文下载。

Data URI 通常生成带 `data` 的对象；远程 URL 通常生成带 `url` 的对象。该类目前是内部数据结构，不是对外 API 响应格式。

### 3.3 Data URI 解码

`_parse_data_uri()` 通过逗号分隔 Data URI 头部和正文，并要求头部包含 `;base64`，正文非空，再调用 `base64.b64decode()` 得到字节。

行为要点：

- MIME 类型从头部读取；无法提取时回退到 `application/octet-stream`。
- 不支持的非 base64 Data URI、空正文和解码异常会被跳过。
- 当前解码使用 `validate=False`，不会严格拒绝所有非标准 base64 字符。
- 解码结果为空时，`extract_images()` 不会创建零字节附件。
- 文件扩展名来自 MIME 子类型，并非通过图片内容识别；复杂 MIME 子类型不一定对应正确的文件扩展名。

**重要限制：** 当前尚不能将 Data URI 解码视为完整的图片校验。它主要完成编码转换，而非验证文件格式、图像可解码性或图像尺寸。

### 3.4 远程 URL 处理

远程 URL 在抽取阶段不会立即下载。当前实现会从 URL 路径末尾提取文件名，并去除查询字符串；没有文件名时回退到 `image.png`。远程图片对象当前使用 `image/png` 作为 MIME 默认值，即使文件名是 `.jpg` 也不代表实际格式已得到确认。

下载被延迟到浏览器发送阶段，具体实现见第 5 节。

## 4. 文本 prompt 与历史图片去重

### 4.1 文本引用是降级机制

图片文本引用由 `_image_reference()` 生成，随后由内容转文本逻辑保留在 prompt 中。

Data URI 示例：

```text
[图片] (image/png, ~442KB base64)
```

远程 URL 示例：

```text
[图片] https://example.com/photo.jpg
```

Data URI 引用只包含媒体类型和估算大小，不含完整 base64 正文。远程 URL 引用则保留 URL 文本。引用让模型至少知道用户提供过图片，但它**不能替代真实的图像附件**。

远程 URL 可能含有签名或其他敏感查询参数，因此不能将引用行视为自动脱敏后的安全文本。需要严格保密的部署应考虑在后续设计中对 URL 引用进行脱敏。

### 4.2 仅抽取本轮新增图片

多轮对话一般会将整个历史消息数组再次提交给桥。如果每次都抽取整段历史，已经粘贴过的图片就可能再次出现在 composer 里。

`_messages_since_last_assistant()` 的规则如下：

1. 找到最后一条 `assistant` 消息。
2. 只保留该消息之后的内容作为本轮新增消息。
3. 如果没有 `assistant` 消息，或最后一条 `assistant` 已经是数组最后一条，则回退为整个消息数组。
4. 同时支持字典消息和对象消息。

调用 `extract_images(messages, only_delta=True)` 时启用这一逻辑。因此正常的历史请求只会对最后一条助手回复之后新增的消息抽图。

这是无状态去重策略：不保存跨请求的图片哈希集合，也不在会话里持久化图片指纹。其正确性依赖调用方提供完整且顺序合理的消息历史；消息截断、顺序不标准或没有 assistant 边界时会进入回退路径，可能再次抽取历史图片。

### 4.3 图片数量上限

`collect_images()` 在开关启用时调用 `extract_images(..., only_delta=True)`，随后按 `IMAGE_PASTE_MAX` 截断列表。

需要注意：当前数量限制发生在抽取函数完成之后。因此它约束的是最终传入浏览器的附件张数，并不是 base64 解码前的输入数量限制，也不是总字节数限制。

## 5. 浏览器端附件粘贴

### 5.1 为什么模拟 paste

网页版 composer 主要是 `contenteditable`。直接调用普通文本填充接口无法把任意文件作为附件交给编辑器。实现借助页面事件处理逻辑：先构造 `File`，再把它放进 `DataTransfer`，最后派发 `ClipboardEvent('paste')`。

### 5.2 远程图片下载

`chatgpt_web/chat_io.py` 中的 `_download_image()` 使用浏览器上下文的 `context.request.get()` 下载远程图片，并通过 `IMAGE_DOWNLOAD_TIMEOUT_MS` 控制超时。

这样可以复用浏览器上下文的网络环境与相关会话状态。请求失败、非成功 HTTP 状态或异常会记录警告并返回 `None`，不会直接让整条文本请求失败。

目前的下载实现并没有形成完整的 URL 安全策略；不要因为它使用了浏览器上下文，就假定它只会访问公开图片站点。安全边界与建议见第 9 节。

### 5.3 合成 paste 事件

`ChatIOMixin._PASTE_IMAGE_JS` 的主要逻辑可概括为：

```javascript
([name, mime, bytesArray]) => {
  const el = document.activeElement
    || document.querySelector('[contenteditable="true"]');
  if (!el) return false;

  const file = new File(
    [new Uint8Array(bytesArray)],
    name,
    { type: mime }
  );
  const dt = new DataTransfer();
  dt.items.add(file);

  el.dispatchEvent(new ClipboardEvent('paste', {
    clipboardData: dt,
    bubbles: true,
    cancelable: true,
    composed: true,
  }));
  return true;
}
```

关键点：

- Playwright `evaluate()` 需要可序列化参数；Python 字节先转换为 `list[int]`，页面内再转换为 `Uint8Array`。
- `DataTransfer.items.add(file)` 将 `File` 放到合成粘贴事件的文件列表中。
- `bubbles`、`cancelable` 与 `composed` 用于让事件以更接近真实粘贴的方式传播并允许编辑器处理。
- 代码先尝试 `document.activeElement`，找不到时再查询 `contenteditable`。

**结果判定边界：** 页面脚本返回 `true` 代表找到了目标元素且执行了事件派发，并不严格证明网页已接受该附件、缩略图已显示或模型最终读到了图片。因此，浏览器端集成验证不能仅以 `dispatchEvent()` 没有异常作为成功标准。

### 5.4 聚焦与发送顺序

`_focus_composer()` 先尝试点击 composer，失败时回退到对元素调用 `focus()`。随后 `_paste_images()` 在循环中处理图片，最后才返回发送流程。

`ReplyWaiterMixin._send_chat_locked()` 的顺序是：

1. 定位输入框并选择当前请求所需的网页模式。
2. 填充文本 prompt，并校验填充结果。
3. 如果传入图片，调用 `_paste_images()`。
4. 调用 `_submit_prompt()` 提交消息。
5. 轮询并提取回复。

图片粘贴必须位于文本填充之后、提交之前。不要将图片粘贴移到提交之后，也不要绕过现有发送流程单独提交一个仅含图片的消息。

### 5.5 字节和浏览器事件失败

单张图片处理失败时会记录警告并继续处理其他图片。若远程下载返回空数据、焦点获取或事件派发抛出异常，该图片不会阻止后续文本消息提交。

同时，当前代码把字节转换为 `list(data)` 传给页面。这种方式实现简单，但对大文件会有明显的内存和序列化开销，因此应结合实际部署的图片大小评估资源限制。

## 6. API 接入与会话流程

### 6.1 Chat Completions

`chatgpt_web/api/chat_adapter.py` 定义 `collect_images()`，统一执行开关检查、增量抽图和数量截断。非流式请求、流式生成以及相关的本地续轮路径都会将图片通过 `driver.send_chat(..., images=...)` 传入发送层。

### 6.2 Responses API

`chatgpt_web/api/responses_adapter.py` 会识别 Responses 风格输入中的图片分片，并在含图片时保留原始 `content` 数组，避免适配阶段过早把分片结构丢掉。

该路径的目的，是让统一的图片提取逻辑能够继续识别图片；不能仅凭适配层保留了 `input_image` 就认定浏览器已经成功收到附件。

### 6.3 流式响应

流式接口与非流式接口共享底层浏览器发送逻辑。图片附件仍在回答开始前附加到同一条网页消息中；SSE 负责把回答增量返回给 API 客户端，不负责传输图片字节。

图片准备失败时，正常的文本发送与回复提取流程仍继续。这也意味着客户端收到有效文本回复，不必然说明该轮图像已被模型成功理解。

### 6.4 重试、播种和续轮

图片参数沿现有 driver 发送链路向下传递，避免单独建立另一套图片提交流程。对于新页面重建后重发的请求，应根据发送链路重新附加本次需要的图片，因为新页面并没有上一页面的 composer 状态。

本轮新增消息筛选用于避免正常多轮请求重复粘贴历史图片，但它不等于浏览器层的事务保证，也没有提供跨请求的持久化附件去重。排查重试时需要分别确认：消息是否已经提交、页面是否已经重建，以及图片是否在新页面上重新附加。

## 7. 配置与默认行为

以下配置定义于 `chatgpt_web/config/_core.py`，并在 `.env.example` 中给出示例：

| 配置 | 默认值 | 行为 |
|---|---:|---|
| `IMAGE_PASTE_ENABLED` | `true` | 总开关。关闭时不收集供粘贴的图片，文本引用仍然保留。 |
| `IMAGE_PASTE_MAX` | `4` | 单轮最多传入浏览器的图片数量。配置在读取时被限制为不小于 0。 |
| `IMAGE_DOWNLOAD_TIMEOUT_MS` | `10000` | 远程图片下载超时，单位毫秒；配置在读取时至少为 500 毫秒。 |

示例：

```dotenv
IMAGE_PASTE_ENABLED=true
IMAGE_PASTE_MAX=4
IMAGE_DOWNLOAD_TIMEOUT_MS=10000
```

行为说明：

- `IMAGE_PASTE_ENABLED=false`：图片不作为真实附件粘贴，prompt 仍保留图片引用文本。
- `IMAGE_PASTE_MAX=0`：列表截断结果为空，等效于关闭真实图片粘贴，但不会自动关闭图片引用文本。
- `IMAGE_PASTE_MAX=1`：只把按输入顺序排列的第一张新增图片传入浏览器。
- 缩短下载超时会更快进入降级路径；延长超时可能增加请求延迟。

目前没有独立的图片总字节数上限、单张图片字节数上限或图片下载响应体大小上限配置。不要将 `IMAGE_PASTE_MAX` 误解为内存或网络流量的完整保护措施。

## 8. 模型能力声明与使用示例

`chatgpt_web/models.py` 中的模型元数据声明输入模态为 `text` 和 `image`，并提供 `text+image->text` 模态描述。这一声明与桥接层接受图片输入相对应。

但模型列表的模态声明不是附件上传成功的遥测结果：实际运行还受 `IMAGE_PASTE_ENABLED`、网页编辑器行为、下载结果和会话状态影响。关闭粘贴功能后，图片请求会降级为文本引用，因此不应将该模式当成真正的图像理解能力。

### 8.1 Chat Completions 请求示例

```json
{
  "model": "chatgpt-web",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "请描述这张图片。" },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/png;base64,BASE64_DATA_HERE"
          }
        }
      ]
    }
  ]
}
```

将 `BASE64_DATA_HERE` 替换为真实图片的 base64 内容。也可以将 URL 替换为符合输入格式的远程图片地址。

### 8.2 Responses 风格分片示例

```json
{
  "type": "input_image",
  "image_url": "https://example.com/photo.jpg"
}
```

该片段示例说明了适配层需要保留的图片数据形态，不代表完整 Responses 请求体，也不代表该 URL 必然能成功下载。

## 9. 安全边界与建议加固

当前实现实现了基本的异常降级，但仍有若干重要安全边界，应在面向不可信客户端或公网部署前评估。

### 9.1 远程 URL 与 SSRF 风险

当前远程地址会进入浏览器上下文的请求。不能假定请求只访问公网图片站点；如果 API 调用者可以提供任意 URL，就应评估其访问本机服务、私网地址、云环境元数据端点或其他受限资源的可能性。

建议在下载前增加明确的 URL 安全策略：

- 只允许明确支持的 `http` 和 `https` scheme，拒绝其他协议。
- 按部署需求拒绝 loopback、私网、链路本地和其他受限制的目标地址。
- 处理重定向时重新执行目标地址验证，避免仅校验初始 URL。
- 将主机名解析结果与实际请求目标纳入安全策略，避免 DNS 重绑定造成校验绕过。
- 根据产品需求使用图片域名白名单或受限的专用下载层。

上述项目是加固建议，不应被误认为现有 `_download_image()` 已经全部实现。

### 9.2 字节大小与资源消耗

当前实现会将 Data URI 解码为完整字节，再转换成整数列表传给浏览器；远程下载则会读取响应体。因此，大型或恶意输入可能消耗大量内存、CPU、网络带宽和浏览器序列化资源。

建议：

1. 在解码前限制 Data URI 长度，并限制单张图片和单请求的累计字节数。
2. 在下载前检查响应头，并对实际读取的响应体设置硬上限；不能只相信 `Content-Length`。
3. 限制提取前的候选数量，避免先解码所有图片再做截断。
4. 为超限输入返回可观察的降级结果，并保持文本请求的既定处理策略。
5. 在资源预算内评估采用更高效的浏览器字节传递机制，降低 `list[int]` 的额外开销。

### 9.3 MIME、文件名与内容验证

Data URI 的 MIME 来自用户提供的头部，远程图片 MIME 当前使用默认值；文件扩展名也不能证明文件实际格式。

建议对允许的图片类型进行白名单校验，并结合文件签名或实际图像解码验证内容。文件名应剥离路径成分及控制字符，不能把输入 URL 中的任意文本直接视为可信文件名。

### 9.4 日志与隐私

当前下载警告只截取 URL 的前一段用于日志，但文本降级引用可能包含完整远程 URL；这两者是不同的隐私边界。

建议在日志、prompt、错误响应和诊断接口中避免暴露完整签名 URL、凭据、完整 base64 或不必要的图片内容。不要为了调试而直接记录整个图片请求正文。

### 9.5 合成事件的可信度

合成 `ClipboardEvent` 与真实用户操作并不完全等价。网页端可能根据浏览器、框架、编辑器版本或可信事件状态采取不同处理路径。测试必须检查实际附件状态，而不是只检查 JavaScript 返回值或没有抛异常。

## 10. 测试策略与验收标准

### 10.1 现有测试位置

当前仓库中与本功能相关的测试入口包括：

- `tests/test_parsing.py`：图片分片降级为文本引用的回归测试。
- `tests/test_streaming.py`：假 driver 记录发送时传入的 `images` 参数，可用于验证流式路径的图片传递。
- `tests/test_responses.py`：假 driver 记录 `images` 参数，可用于验证 Responses 相关路径的图片传递。
- `tests/test_config.py`：配置解析、配置快照和默认值相关测试入口。

这些测试不能单独证明真实网页版已经接受并理解了图片附件。建议为图片抽取和浏览器粘贴补充专门测试，不要仅依赖现有协议测试。

### 10.2 建议的单元测试

| 测试范围 | 用例 | 预期行为 |
|---|---|---|
| Data URI 解析 | 有效 base64 PNG/JPEG | 得到正确 MIME 和原始字节。 |
| Data URI 解析 | 缺失逗号、非 base64、空 payload、解码异常、零字节结果 | 跳过图片，不构造空附件。 |
| 分片兼容 | 标准 `image_url` 字典、字符串形式、`input_image`、裸 `url`/`image` | 按约定提取地址。 |
| URL 处理 | 带查询字符串的 URL、无文件名 URL | 文件名提取符合当前规则；未来安全校验拒绝受限目标。 |
| 文本引用 | base64 图片与远程图片 | 输出引用，不泄漏完整 base64。 |
| 增量筛选 | 历史中有 assistant，后面有新增图片 | 只提取最后一条 assistant 后的图片。 |
| 增量筛选 | 无 assistant 或最后一条 assistant 在数组末尾 | 符合回退规则。 |
| 数量与开关 | 开关关闭、上限为 0、上限为 1、超过默认上限 | 附件列表按预期被禁用或截断。 |
| 远程下载 | HTTP 非成功、超时、异常、空响应 | 跳过对应图片并继续文本提交。 |
| 粘贴执行 | 页面无 composer、焦点失败、evaluate 抛异常 | 不因图片失败而阻止文本发送。 |
| 调用路径 | Chat Completions、流式、Responses 相关入口 | 附件参数正确传递到 driver。 |

### 10.3 建议的真实浏览器集成测试

在隔离测试账户和可控图片源上至少验证：

1. 单张 PNG 与 JPEG 粘贴后，composer 中确实出现附件预览或站点对应的附件状态。
2. 文本和附件最终属于同一条消息。
3. 含图片问题的模型回复能够利用图片内容，而不是只复述 `[图片]` 引用行。
4. 两张图片均按顺序附加；超出数量上限的图片不会被粘贴。
5. 远程图片下载失败时仍能收到正常的文本请求回复。
6. 连续多轮重复携带历史图片时，不会在正常路径中重复附加历史附件。
7. 页面重建或重试时，新页面能按发送流程重新附加本次需要的图片。
8. 图片功能关闭时不派发附件事件，但仍保留降级引用。

### 10.4 本地回归验证

建议先运行相关定向测试，再运行项目完整测试集：

```bash
.venv/bin/python -m pytest tests/test_parsing.py tests/test_streaming.py tests/test_responses.py tests/test_config.py
.venv/bin/python -m pytest
```

测试结果应以实际执行输出为准。若缺少专门的图片抽取或浏览器测试，应先补齐相应测试，再将真实图片上传能力视为完整验收。

## 11. 故障排查

| 现象 | 检查方向 |
|---|---|
| 模型只看到 `[图片]`，没有理解图像内容 | 检查开关、数量上限、分片结构、Data URI 是否可解码，以及网页中是否真的出现附件。 |
| 远程图片没有出现 | 检查 URL、HTTP 状态、下载超时、重定向、目标站点访问权限和日志中的下载警告。 |
| 文本发送成功但图片缺失 | 这是当前允许的降级行为；检查粘贴异常、composer 焦点、编辑器是否接受合成事件。 |
| 同一图片多次出现 | 检查消息顺序、最后一条 assistant 边界、请求是否仅包含部分历史，以及是否进入无状态筛选的回退路径。 |
| 只有前几张图片被上传 | 检查 `IMAGE_PASTE_MAX` 是否达到上限。 |
| 大图片导致请求延迟或资源占用高 | 当前没有完整的图片字节数上限，需检查输入规模，并考虑落实第 9 节的限制措施。 |
| 浏览器重建后图片丢失 | 检查重试时 `images` 是否继续向下传递，以及是否在新页面提交前重新粘贴。 |

## 12. 实施原则与后续工作

本功能应维持以下不变量：

1. 图片正文不能被展开到普通文本 prompt 中。
2. 图片必须在文本填写完成后、同一条消息提交前附加。
3. 历史图片不能在正常多轮路径中无条件反复粘贴。
4. 图片处理失败不能悄悄被描述为上传成功；日志和测试要区分“事件派发”与“网页确认附件”。
5. 图片上传开关、数量上限和下载超时应有明确文档与测试覆盖。
6. 对不可信输入的远程下载、字节规模、MIME 和文件名需要明确的安全约束。

建议后续按优先级完善：

- **优先级高：** 增加 URL/SSRF 防护、单图与累计字节上限、响应体大小限制。
- **优先级高：** 为 Data URI 解析、增量筛选、配置开关和数量截断补充专门单元测试。
- **优先级高：** 增加浏览器集成测试，核实附件实际出现并能被模型读取。
- **优先级中：** 校验 MIME 与图片真实格式，规范文件名和下载重定向处理。
- **优先级中：** 对远程 URL 的文本引用、日志和诊断信息增加脱敏策略。
- **优先级中：** 增加可观测性，区分候选图片数、下载成功数、事件派发数，以及经网页验证的附件成功数；避免将目前的事件派发计数误解为端到端成功率。

以上方案保留当前项目的文本优先、附件尽力上传、失败可降级的设计，同时明确其实际能力与尚待加固的安全边界。