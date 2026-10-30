# 提示词结构改造任务清单

依据 `doc/prompt_design.md` 分解。目标是让提示词的**三块结构（契约 → 任务 → 纠偏）**、
**英文区段标题**、**精简文本**三件事全部落地，并有测试与真机证据守护。

> 状态标记：`DONE` / `TODO` / `BLOCKED` / `WONTFIX`。
> 优先级：`P0`（结构正确性）/ `P1`（体验与稳健性）/ `P2`（清理与统计）。

---

## 阶段 0：现状盘点（已完成，作为基线）

### T0.1 三块结构与英文标题已落地

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-30） |
| 优先级 | P0 |
| 来源 | design §1、§2、§3 |

**已完成**：

- `prompting.py` 新增 `SECTION_USER = "[User Request]"`、`SECTION_TOOL_RESULTS = "[Tool Results]"`；
- `_render_message` 的 system / assistant / user 前缀全部改为英文（`[System]` / `[Your Previous Reply]` / `[User Request]`）；
- 工具结果标题改为 `[Tool Results <tool_call_id>]`；
- 契约块仍在最前（增量位置 0 / 播种贴任务前），纠偏块仍在最后。

**验收**：`tests/test_prompting.py`、`test_seed_prompt.py`、`test_tool_injection.py` 全绿。

### T0.2 提示词文本精简已落地

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-30） |
| 优先级 | P0 |
| 来源 | design §4.1.3 |

**已完成**：契约块身份段、规则 1–9、强调块、首轮纠偏块、`EMPTY_TOOL_RESULT_NOTE`、
`format_repeat_call_hint` 全部压缩；所有承重短语（`only way you take real action`、
`No tool has to be installed or mounted on your side`、`Never re-run the exact same command` 等）
保留原文，测试断言未削弱。

---

## 阶段 1：结构不变量补测（P0）

### T1.1 新增「三块顺序固定」测试

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（测试已通过；人工故障注入验证待补） |
| 优先级 | **P0** |
| 来源 | design §6 I10 |

**背景**：design §6 把「三块顺序固定为 契约 → 任务 → 纠偏」列为 I10，但目前没有专门断言。
现有 `test_incremental_tools_block_stays_first` 只覆盖契约在最前，没有覆盖纠偏在最后、
也没有覆盖「任务夹在两者之间」。

**实施要点**：

1. 在 `tests/test_tool_injection.py` 新增 `test_sections_order_is_contract_task_hint`：
   - 构造「带工具 + 工具结果为空 + 同一命令重复 2 次」的 prompt；
   - 断言 `text.index(TOOLCALL_HEADER) < text.index(SECTION_TOOL_RESULTS) < text.index(REPEAT_HEADER)`。
2. 新增 `test_user_request_section_precedes_repeat_hint`：无工具结果的增量轮 + 重复空调用时，
   断言 `[User Request]` 出现在 `[Repeated Empty Tool Call]` 之前。

**验收**：

- [ ] 两条新测试通过；
- [ ] 故意把 `parts.append(format_repeat_call_hint(...))` 改成 `insert(0, ...)` 时测试失败（人工验证一次）。

### T1.2 新增「第 2 块内部互斥」测试

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（正反向用例已通过） |
| 优先级 | **P0** |
| 来源 | design §1.2 |

**背景**：design §1.2 要求「有工具结果就不发 `[User Request]`」，但当前实现依赖
「最后一条 assistant 之后」的切分，没有显式断言畸形输入（旧 tool 结果 + 新 user）不会两个都发。

**实施要点**：

1. 构造消息：`user → assistant(tool_calls) → tool → user`（新 user 之后无新工具结果）。
2. 断言 prompt 里 `[Tool Results]` 不出现（旧结果被排除）、`[User Request]` 只出现一次。
3. 反向用例：`user → assistant(tool_calls) → tool`（无新 user）时只出现 `[Tool Results]`。

**验收**：

- [ ] 两条测试通过；
- [ ] 若当前实现对畸形输入仍同时输出两块，先修 `build_prompt` 再补测（见 T1.3）。

### T1.3 畸形输入下的互斥保障

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（`_run_messages` 已过滤旧 tool 结果；播种路径回归由全量测试覆盖） |
| 优先级 | P0 |
| 来源 | design §1.2 |

**背景**：T1.2 若发现同时输出，需要在 `build_prompt` 里显式丢弃「新 user 之前的旧 tool 结果」。

**实施要点**：在 `_run_messages` 或 `build_prompt` 里，定位最后一条 user 下标，丢弃其之前的
tool 消息（保留其后的）。注意不要影响播种路径（播种要完整重放）。

**验收**：

- [ ] T1.2 的畸形输入用例通过；
- [ ] 播种路径测试不回归。

---

## 阶段 2：契约块内容守护（P0/P1）

### T2.1 新增「身份链路段承重短语」专项测试

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-30） |
| 优先级 | **P0** |
| 来源 | design §4.1.1 |

**背景**：design §4.1.1 记录两种旧措辞导致拒答。现有
`test_instruction_denies_the_no_tools_refusal` 覆盖了大部分，但**没有断言「不得出现
'You have NO direct access' 这类否定式能力断言」**。精简时容易把否定式写回来。

**实施要点**：在 `test_instruction_denies_the_no_tools_refusal` 追加：

```python
for banned in (
    "NO direct access",
    "ALREADY MOUNTED",
    "autonomous agent",
    "you do not have access",
):
    self.assertNotIn(banned, text)
```

**验收**：

- [ ] 断言通过；
- [ ] 手动把身份段改回旧措辞时测试失败。

### T2.2 新增「精简不丢约束」清单测试

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-30） |
| 优先级 | **P1** |
| 来源 | design §4.1.3 |

**背景**：design §4.1.3 强调「被删的语义点以最短形式保留，并由测试守护」。目前这些断言
分散在多个测试里，缺少一份**集中清单**，精简时容易漏。

**实施要点**：新增 `test_contract_keeps_all_load_bearing_constraints`，用一张表断言契约块
必须包含的全部语义点：

| 语义点 | 断言片段 |
| --- | --- |
| 载体是围栏 | `` `tool_call` `` |
| 参数类型 | `declared types` |
| 单行转义 | `escape quotes` |
| 命令进 command | `command` value |
| 最多三条 | `THREE tool_call blocks` |
| 只输出围栏块 | `ONLY the fenced blocks` |
| 禁止无工具拒答 | `have no tools` |
| 空输出是成功 | `EMPTY tool result` |
| 禁止 XML/DSL | `XML / DSL tags` |

**验收**：

- [ ] 测试通过；
- [ ] 每条断言在删掉对应规则时失败。

### T2.3 契约块长度上限测试

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-30） |
| 优先级 | P1 |
| 来源 | design §4.1.3（精简目标） |

**背景**：精简的目的之一是控制 prompt 体积，但没有量化护栏，后续改动可能重新膨胀。

**实施要点**：新增 `test_contract_block_stays_compact`，断言
`len(format_tools_instruction([bash_tool])) < 2600`（当前实测约 2400，留 10% 余量）。
阈值写进测试注释并注明「调高需说明理由」。

**验收**：

- [ ] 测试通过；
- [ ] 阈值有注释说明来源。

---

## 阶段 3：真机验证（P0，当前 BLOCKED）

### T3.1 E2E C1/C2 对比（顺序 + 精简的联合效果）

| 项 | 内容 |
| --- | --- |
| 状态 | `BLOCKED`（需真实网页，按 design §10 与 `doc/tasks.md` T4.7 用户决定暂不连跑） |
| 优先级 | **P0** |
| 来源 | design §1.1、§10；`doc/tasks.md` T1.1 |

**背景**：design §1.1 的顺序选择基于 T1.1 的实测，但那次实测是**旧文本**（未精简、无英文标题）。
现在文本变短了，调用率可能变化，必须重测。

**实施要点**：

1. 按 `doc/e2e_test_design.md` 前置条件，带 `E2E_PORT=8002` 跑：
   `pytest tests/e2e/test_parity.py -k "c1 or c2" -v -s`；
2. 连续跑 2 次，记录每次的 PASS/FAIL 与耗时；
3. 结果写进 `doc/update.md` 新增小节（注明：精简后 + 英文标题后）。

**验收**：

- [ ] 两次运行均 PASSED；
- [ ] 结果表已写入 `doc/update.md`；
- [ ] 若 FAILED，回退到 T3.2 的 A/B 方案定位原因。

### T3.2 契约位置 A/B（放前 vs 放后）

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（开关已实现 + 单测；A/B 真机数据待 T3.1 解封） |
| 优先级 | P1 |
| 来源 | design §10「契约放前 vs 放后」 |

**背景**：DeepseekBridge 把契约放最后，本项目放最前，两者各有实测依据但**不在同一套用例上比较过**。

**实施要点**：

1. 增加环境变量 `PROMPT_CONTRACT_LAST`（默认 false）；
2. 为 true 时增量路径改为 `parts.append(format_tools_instruction(...))`；
3. 用同一套 E2E C1/C2 各跑 3 次，记录调用率；
4. 结论写进 design §1.1，并决定是否保留该开关。

**验收**：

- [ ] A/B 数据表写入 design §1.1；
- [ ] 若「放后」不劣于「放前」，讨论是否切换默认。

### T3.3 区段标题复述率统计

| 项 | 内容 |
| --- | --- |
| 状态 | `BLOCKED`（需真机多轮样本；当前无可用且获授权的隔离测试会话） |
| 优先级 | P2 |
| 来源 | design §10「区段标题的模型理解度」 |

**实施要点**：在多轮真机对话后检查模型回答里是否出现 `[User Request]` / `[Tool Results`
字面量。若出现，考虑给标题加「以下为背景说明，无需回应」后缀。

**验收**：

- [ ] 统计 ≥10 轮，记录复述次数；
- [ ] 结论写入 design §10。

**阻塞原因**：本项需要真实网页对话样本，不能用单元测试或模拟数据替代。按 `doc/tasks.md` T4.7
既有决定，暂不启动 E2E；待用户明确授权并准备独立登录态的测试 profile 后再执行。

---

## 阶段 4：清理（P2）

### T4.1 删除遗留兼容函数

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（全仓检索无定义及有效引用；全量测试通过） |
| 优先级 | P2 |
| 来源 | design §10（旧版遗留） |

**背景**：经检查 `chatgpt_web/` 与 `tests/` 中已无 `with_tool_instructions` /
`render_tool_result` 的引用，但不确定外部调用方。

**实施要点**：

1. 全仓（含 `skills/`、`README.md`）grep 确认无引用；
2. 若有，先加 deprecation warning 一个版本，再删；
3. 更新 `doc/prompt_design.md` §9 实现位置表。

**验收**：

- [ ] grep 无引用；
- [ ] 全量测试通过；
- [ ] design 文档同步。

### T4.2 标题常量集中到单一模块

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（全部标题常量集中在 `toolcalls.py`） |
| 优先级 | P2 |
| 来源 | design §2（「唯一用途是让模型区分段落语义」） |

**背景**：区段标题目前分散在 `prompting.py`（`SECTION_USER`、`CONTEXT_REBUILD_HEADER`）
与 `toolcalls.py`（`TOOLCALL_HEADER` 等）两处。design §2 列了一张总表，但代码里没有单一真源。

**实施要点**：把全部标题常量迁到 `toolcalls.py`（或新建 `prompt_sections.py`），
`prompting.py` 从那里导入。避免循环导入需注意 `prompting` → `toolcalls` 的现有依赖方向。

**验收**：

- [ ] 所有标题常量单点定义；
- [ ] design §2 表格与代码一致；
- [ ] 全量测试通过。

### T4.3 文档与代码一致性测试

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（代码/文档同步测试已通过） |
| 优先级 | P2 |
| 来源 | design §2、§9 |

**背景**：design §2 的标题表与 §9 的实现位置表容易与代码漂移。

**实施要点**：新增 `test_doc_titles_match_code`（放在 `tests/test_doc_sync.py`，该文件已存在）：
解析 design §2 表格里反引号包裹的常量名，断言它们在对应模块里存在且值一致。

**验收**：

- [ ] 测试通过；
- [ ] 故意改错一个标题值时测试失败。

---

## 阶段 5：体验优化（P2，待真机反馈）

### T5.1 空输出说明措辞再评估

| 项 | 内容 |
| --- | --- |
| 状态 | `BLOCKED`（缺少当前措辞与候选措辞的真机对照数据） |
| 优先级 | P2 |
| 来源 | design §10 |

**背景**：`EMPTY_TOOL_RESULT_NOTE` 已精简为英文 3 句，但「更短是否同样有效」未验证。

**实施要点**：在获授权的真机运行中同时观察模型是否重发空输出命令。先记录当前措辞基线，再尝试
压缩一句并用相同用例重测；若重复率上升，回退。不得把没有样本解释为重复率为 0。

**验收**：

- [ ] 有前后对比数据；
- [ ] 结论写入 design §10。

**阻塞原因**：依赖真实网页对照实验；E2E 尚按 `doc/tasks.md` T4.7 用户决定暂停。

### T5.2 纠偏有效率统计

| 项 | 内容 |
| --- | --- |
| 状态 | `BLOCKED`（日志实现已完成；≥20 个真机纠偏样本尚未采集） |
| 优先级 | P2 |
| 来源 | design §7 |

**实施要点**：`chat_io.send_chat` 已记录 `nudge_fired`、`nudge_recovered`、`nudge_failed`。
在获授权的真机多轮后统计「重发一次后拿到合法调用」的比率，并明确分母与样本筛选口径。

**验收**：

- [x] 日志字段落地；
- [ ] 至少 20 次纠偏样本的统计结果写入 design §10。

**阻塞原因**：日志代码完成不等于已有真实样本；当前不能虚构统计值。

---

## 依赖关系与建议执行顺序

```
T1.1 ─┐
T1.2 ─┼─→ T1.3
T2.1 ─┤
T2.2 ─┘
         ↓
       T3.1（真机，BLOCKED）
         ├─→ T3.2（失败时）
         └─→ T3.3
         ↓
       T4.1 / T4.2 / T4.3（清理，可并行）
         ↓
       T5.1 / T5.2（依赖真机数据）

**当前真机证据待办**：T3.1（C1/C2 两次 E2E）、T3.2（契约位置 A/B）、T3.3（≥10 轮标题复述统计）、
T5.1（空输出说明前后对照）、T5.2（≥20 个纠偏样本）。这些项目均不得以单测通过代替真机证据。

**安全边界**：E2E 需使用独立且已登录的测试 profile，并按 `doc/e2e_test_design.md` 配置独立端口。
不得终止、接管或复用其他项目正在使用的浏览器进程或 `user_data`。
```

**建议先做**：T1.1、T1.2、T2.1、T2.2（纯单测，无外部依赖，能立刻发现结构回退）。
**T3.1 是唯一需要真实网页的任务**，按 `doc/tasks.md` T4.7 的用户决定保持 `BLOCKED`，
直到显式安排连跑。
