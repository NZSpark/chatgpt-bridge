# 宽泛异常审计（PI-013）

> 审计范围：`chatgpt_web/chat_io.py`、`completion.py`、`server.py`、`responses.py`、`toolcalls.py`、`tasks.py`、`page_pool.py`、`session_store.py`。
> 原则：`except Exception` 只允许出现在“降级 / 兼容 / 资源清理 / 最终边界”位置；所有这些位置都必须保留可观测性，不能把业务错误静默吞掉。

## 审计结论

本轮审计没有发现必须改成 `except BaseException`、也没有发现应捕获 `Exception` 后重新伪装成成功结果的路径。剩余宽泛捕获均属于以下四类：

1. **兼容性 fallback**：Playwright DOM 操作失败后尝试下一种定位、输入或清理方法。
2. **诊断 / best-effort**：单个 selector、DOM 节点或轮询采样失败时继续收集其它诊断信息。
3. **资源 / 持久化容错**：日志输出目录、session/task snapshot 写入失败不应直接杀死服务；失败必须记录 warning。
4. **边界兜底**：HTTP handler、工具 executor 等最外层捕获未知异常，转换为明确的错误响应或结构化 tool failure。

## chat_io.py

- `_prune_output_dir()`：文件系统清理是 best-effort；单文件 `stat/unlink` 失败不应阻止其它文件清理。目录扫描失败直接结束清理。异常不会影响请求主路径，因此允许忽略，但必须保持静默失败范围仅限“清理”。
- 工具纠偏重发：已知上游异常单独处理；最后的 `except Exception` 是兼容性兜底，记录 warning 并返回首轮回复，避免纠偏机制反过来把正常答案变成 500。
- `_dispatch_enter()` / `_read_input_text()` / `_fill_prompt()` / `_clear_input()`：这些调用位于多策略 DOM fallback 内。单个 Playwright 操作失败会触发下一种策略；输入失败会带 attempt 日志，清理失败在最终残留文本检查中产生 warning。
- 发送前读取旧 assistant 节点、轮询读取单个 assistant node：单节点读取失败不应阻断整个轮询；继续寻找其它节点，最终状态由 end-detection 决定。

## completion.py

- `_warn_if_blocked()`：启动自检属于诊断路径；页面探测本身失败时不能阻止服务启动，因此返回并把真正的初始化错误留给 driver/healthz。
- `_page_shows_context_limit()`：上下文上限探测是辅助信号；DOM 读取失败必须视为“没有可靠证据”，而不是误判为到顶。
- `_recover_session()` / 新会话操作：恢复路径是 best-effort；失败记录 warning 并由上层继续按统一失败路径处理。

## server.py

- `lifespan()`：浏览器初始化属于服务可降级启动项；捕获未知初始化异常后写入 `driver.init_error`，并通过日志与 `/healthz` 暴露。
- `/debug` selector probe：每个 selector 独立采样；单个 selector 异常写入该 selector 的 `error` 字段，不影响其它 selector 的诊断。
- DOM diagnostic node summary：单节点 text/class 获取失败分别降级为空值，避免诊断接口本身因坏句柄失效。
- Chat request 最外层：最终安全边界，记录完整 traceback 并返回 `500 server_error`；不能让未知异常穿透 ASGI。
- 本地工具执行后继续生成：这里是明确的操作边界；未知异常记录 traceback 并返回错误响应，不能继续伪装成成功。

## responses.py

- Responses request 最外层：未知异常打印 traceback 后映射为 `500 server_error`，属于协议边界兜底。
- Responses streaming worker：worker 内未知异常必须进入已有 `_map_exception()` / queue error 路径，防止后台 task 异常直接丢失。

## toolcalls.py

- 工具 executor：工具实现的任意运行时错误都必须转换为结构化 failure，并写入 execution ledger；这是 runtime 隔离边界。
- JSON / 参数解析 helper：这里故意捕获 JSON 解码异常，因为“模型输出不是合法 JSON”本身是正常输入分支，需要 fallback / repair，而不是崩溃。
- validator / executor pipeline：validator 和 executor 的未知错误统一包装为 `ToolCallValidationError` / `ToolCallExecutionError`，保留原异常作为 cause；serialization 由后续边界继续验证。

## tasks.py

- snapshot `load()`：文件不存在、JSON 损坏、历史格式异常都必须回退为空任务状态；同时记录 warning + `exc_info=True`，避免污染下一轮任务。
- snapshot `save()`：写盘失败不能阻止当前请求继续；记录 warning + `exc_info=True`，下一轮自动重试。

## page_pool.py

- `page.close()`：资源回收必须 best-effort；关闭失败记录 warning，不让清理异常覆盖真正的请求结果。
- ready selector probe：页面已打开但探测失败时记录 warning 并返回 false，交给上层恢复逻辑。

## session_store.py

- 状态文件读取 / JSON parse：坏状态必须安全降级为空状态，同时只首次 warning（避免每次请求刷屏）。
- 状态写盘：失败必须 warning + traceback，当前内存状态仍可继续工作；下一次状态写入再尝试。

## 可观测性要求

所有宽泛捕获必须满足至少一个条件：

- 有 `warning/error` 日志并包含异常对象；
- 或将异常作为结构化字段 / error type 返回；
- 或立即进入下一级 fallback，并在最终失败处记录可操作的 warning/error。

本审计中没有发现“捕获 `Exception` 后无条件吞掉、同时又继续声称操作成功”的业务路径。