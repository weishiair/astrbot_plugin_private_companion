# JEV 优化机会清单与接入指南

本文面向继续开发 `astrbot_plugin_private_companion` 的维护者，回答三个问题：

1. 哪些现有模型调用适合交给 JEV；
2. 哪些只能由 JEV 做前置过滤，不能完整替代原模型；
3. 新接线如何保持可回退、可观测、可灰度。

本文是机会清单和实施路线，不替代 [JEV 判定委托设计](./jev-decision-delegation.md)。端点、原语、阈值、熔断、预热和记账的权威说明仍以后者及 `domains/decision/*` 为准。

## 1. 结论摘要

当前生产代码中已有 6 个 JEV 接线点：4 个默认开启、2 个默认关闭。下一项最值得实现的是 `group_question_wakeup_reply_review`，因为它当前用一次 120-token 模型调用只产生 `send/drop`，决策与文本生成已经完全分离。

随后可考虑两个“只减少调用、不负责生成”的前置过滤器：

| 优先级 | 场景 | 推荐方式 | 原因 |
|---|---|---|---|
| P0 | 群答疑回复发送前复核 | `noul` 完整接管，默认关闭并先影子校准 | 输出只有 `send/drop`，原模型不生成正文 |
| P1 | QQ 空间评论回复决策 | `noul` 只接管“明确跳过”；需要回复时仍跑原模型生成正文 | 当前 120-token 调用同时判断并生成评论 |
| P1 | 外界信息主动分享意愿 | `noul` 或 `score` 只接管“明确不分享”；可能分享时仍跑原模型 | 当前 360-token 调用还要生成 motive/tone/boundary |
| P2 | 主动人格判定 | 仅影子评估或低风险前置过滤 | 需要 `rewrite/defer/drop` 及计划字段，不能完整替代 |
| 暂缓 | 主动消息终审 | 仅研究，不接生产决策 | 终审还承担改写、事实边界和泄漏收敛 |

群成员安全、复杂情绪判断、视觉审核、文本生成以及已有零成本确定性规则，不应为了“统一走 JEV”而迁移。

## 2. 适用判据

一个场景只有同时满足下列条件，才适合完整交给 JEV：

| 判据 | 必须满足的要求 |
|---|---|
| 输出形态 | 最终行为只依赖布尔值、单标签或 0–100 分数 |
| 输入形态 | 判断依据可以完整、安全地放入文本 `state`；不依赖图片、音频或工具执行结果的视觉细节 |
| 生成分离 | 下游不需要本次调用同时生成回复、改写稿、理由证据或结构化计划 |
| 回退存在 | JEV 返回 `None`、超时、低置信度或熔断时，可以执行现有模型/规则路径 |
| 延迟允许 | 热连接约 0.25–0.31 秒；任务预算需给网络抖动和回退留出余量 |
| 误判可控 | 错误不会直接造成封禁、安全漏判、隐私泄漏或不可逆外部操作 |
| 调用收益 | 被替代的模型调用成本明显高于新增 JEV 调用；原本为正则/哈希时通常没有收益 |

只满足“输出中包含一个布尔字段”并不够。例如一个调用同时返回 `decision` 和改写正文时，JEV 最多只能做前置过滤，不能替代整个调用。

### 2.1 三种接入模式

| 模式 | 采用条件 | 运行方式 |
|---|---|---|
| 完整接管 | 原调用只做离散判断 | JEV 有明确结论时直接采用；`None` 回落原路径 |
| 单向前置过滤 | 原调用还负责生成文本 | 仅在 JEV 得出一个安全、保守的方向时提前结束；另一方向和 `None` 都继续原模型 |
| 影子评估 | 风险高或尚未校准 | 同时记录 JEV 与原模型结论，不改变生产行为；积累样本后再决定是否接管 |

## 3. 当前任务全表

任务注册表位于 `domains/decision/jev_tasks.py`。`runtime_wired=False` 的任务即使出现在配置中也会 fail closed，不应发起网络请求。

| 任务 | 原语 | 当前状态 | 默认 | 被替代/过滤的原路径 | 结论 |
|---|---|---|---|---|---|
| `group_followup_judge` | `noul` | 已接线 | 开 | 8-token `YES/NO` | 完整接管 |
| `group_air_reply_guard` | `noul` | 已接线 | 开 | 8-token `REPLY/SILENCE` | 完整接管 |
| `smart_silence` | `noul` | 已接线 | 开 | 100-token `send/silent` JSON | 完整接管 |
| `group_interject` | `noul` | 已接线 | 开 | 140-token 判断并生成插话正文 | 只在明确不插话时前置结束 |
| `smart_message_debounce` | `noul` | 已接线 | 关 | 80-token `complete/incomplete` JSON | 完整接管；0.8 秒总预算偏紧 |
| `rest_wakeup_judge` | `score` | 已接线 | 关 | 180-token 休息唤醒评分 | 完整接管；连续分数置信度需单独校准 |
| `group_member_safety` | `noul` | 已注册、未接线 | 关 | 280-token 风控 JSON | 不完整迁移；需要 category/severity/evidence |
| `group_wakeup_context` | `noul` | 已注册、未接线 | 关 | 本地正则评分 | 不迁移；原路径零模型成本 |
| `proactive_persona_judge` | `choice` | 已注册、未接线 | 关 | 260-token 主动计划判断 | 仅影子/前置过滤；仍需改写计划字段 |
| `emotion_judgement` | `choice` | 已注册、未接线 | 关 | 180-token 情绪复核 JSON | 不完整迁移；需要多字段与关系边界证据 |

## 4. 推荐候选

### 4.1 P0：群答疑回复发送前复核

- 位置：`main.py::_review_group_question_wakeup_reply_before_send`
- 当前成本：一次最多 120 token 的模型调用。
- 当前输出：`{"decision":"send|drop","reason":"..."}`。
- 推荐任务名：`group_question_wakeup_reply_review`。
- 推荐原语：`noul`。
- 推荐问题语义：`true = 应发送`，`false = 应拦截`。
- 推荐初始配置：`default_enabled=False`，预算 1.6 秒，`min_confidence` 先取 0.60。

这是最干净的下一项，因为待发送回复已经由上游生成，本方法只决定是否放行；`reason` 仅用于日志，不参与业务计算。接入点应位于 provider 检查之前，使未配置复核模型时也能使用 JEV。

回退契约：

- `True`：返回固定理由的 `send`；
- `False`：返回固定理由的 `drop`；
- `None`：原样执行现有 120-token 复核；
- 开关默认关闭，先影子比对群聊公共求助、吐槽、反问、接群友话等样本。

主要风险是误放行造成 Bot 碰瓷插话，或误拦截真实公共求助。上线前应分别统计 `send→drop` 与 `drop→send` 的分歧，而不能只看总一致率。

### 4.2 P1：QQ 空间评论回复前置过滤

- 位置：`qzone_comments.py::_qzone_decide_comment_reply`
- 当前成本：一次最多 120 token 的模型调用。
- 当前输出：`decision=reply|skip`，并在 `reply` 时生成 8–45 字正文。
- 推荐任务名：`qzone_comment_reply_prefilter`。
- 推荐原语：`noul`。
- 推荐问题语义：`true = 值得进入回复生成`，`false = 明确不需要回复`。

JEV 不能生成公开评论，所以不能完整替代。推荐单向前置过滤：

- JEV 明确返回 `False`：直接 `skip`；
- JEV 返回 `True` 或 `None`：继续原模型，让原模型同时复核公开边界并生成正文；
- 不允许把 JEV 的标签或概率拼成回复文本；
- 保留现有 `_qzone_comment_reply_leaks_private` 和长度校验。

收益取决于评论中“纯表情、路过、点赞、无意义短句”的比例。若大部分评论都值得回复，前置过滤只会增加延迟，应根据真实审计样本决定是否启用。

### 4.3 P1：外界信息主动分享意愿前置过滤

- 位置：`news_exploration.py::_build_external_event_wish`
- 当前成本：一次最多 360 token 的模型调用。
- 当前输出：`relevance`、`desire`、`should_share`、`share_probability`，以及 `self_link/motive/tone/boundary` 文本字段。
- 推荐任务名：`external_event_share_prefilter`。
- 推荐原语：优先 `noul`；需要排序时可试 `score`。

推荐只接管“明确不值得分享”的方向：

- JEV 明确否定：返回不分享，不再生成内部动机文本；
- JEV 肯定或不确定：继续原模型生成完整 wish；
- 生活福利等已有本地强规则命中时，继续优先走本地规则，不调用 JEV；
- 用户配置的 relevance/desire override 仍由原路径处理，不交给 JEV 擅自覆盖。

该场景单次节省潜力高，但输入上下文长，必须控制 `state` 在 JEV 上限内，只提供标题、摘要、来源类型、当前状态摘要和稳定兴趣摘要，不能把完整记忆检索结果无界发送。

### 4.4 P2：主动人格判定

- 位置：`proactive_engine.py::_review_planned_proactive_with_model`
- 当前任务：`proactive_persona_judge`，已经注册但未接线。
- 当前成本：一次最多 260 token 的模型调用，并有缓存与每日上限。
- 当前输出：`send/rewrite/defer/drop`、score、delay、reason/action/topic/motive 等计划字段。

该任务不适合完整替代：JEV 可以选择标签或给适合度打分，但不能生成 `rewrite` 所需的计划字段。第一阶段只建议影子评估；若后续接线，应限定为：

- 明确低分时给出“继续原模型复核”或保守 defer 建议，不直接永久 drop；
- 高分不能自动绕过所有原模型复核，因为原模型还负责人格、时效和计划字段收敛；
- `rewrite`、`delay_minutes`、topic/motive 仍由原模型或确定性规则产生；
- 保留现有缓存和每日调用上限，避免 JEV 与原模型双重放大调用量。

### 4.5 暂缓：主动消息发送终审

- 位置：`proactive_message.py::_review_proactive_message_send_decision`
- 当前成本：一次最多 220 token。
- 当前输出：`send/rewrite/drop`，必要时接受本地 `defer`，并可能生成改写正文。

这条链路承担事实来源、内部信息泄漏、平台链接、人格表达和改写验收，且前面已有大量本地快判。剩下进入模型的样本通常正是难例，JEV 前置判断的边际收益不高。建议只做离线/影子研究，不在当前路线中注册生产任务。

## 5. 明确不迁移的场景

| 类别 | 代表任务 | 不迁移原因 |
|---|---|---|
| 高风险风控 | `group_member_safety` | 需要恶意类别、严重度、目标、上下文证据；误判可能累计 strike 或静默用户 |
| 多维情绪/关系 | `emotion_judgement` | 需要 event、target、intensity、severity、interaction_type、关系 tier，单标签不足以驱动策略 |
| 零成本确定性规则 | `group_wakeup_context`、重复消息哈希、冷却/额度/权限判断 | JEV 会把本地零成本路径变成有费用、有网络失败面的路径 |
| 视觉判断 | 生图投递审核、反应图视觉复核、衣柜图片理解、私聊图片转述 | JEV 只接收文本，无法检查真实像素 |
| 文本生成/改写 | `atrelay_rewrite`、`reactive_poke_reply`、`response_review`、语音文案、照片 prompt、空间评论正文 | JEV 不生成自然语言 |
| 长结构生成 | 日程、日记、梦境、创作、记忆/群聊 episode、世界书印象 | 输出不是离散判断，且需要事实整合和连续文本 |
| 选择后仍需生成 | `news_digest`、`web_exploration_query/digest`、`creative_review` | 即使 JEV 能选标签/候选，原模型仍要生成 headline、note、query、issues 等内容，难以省掉整次调用 |
| 连通性测试 | `provider_test`、JEV probe | 目标是验证真实提供商/端点，不能用另一判断模型代替 |

## 6. 接线规范

### 6.1 注册任务

在 `domains/decision/jev_tasks.py` 增加唯一任务名和 `JevTaskSpec`：

- 新任务一律先 `default_enabled=False`；
- 未完成生产调用点前保持 `runtime_wired=False`；
- 原语只选 `noul`、`choice`、`score` 中真正匹配业务输出的一种；
- 预算必须小于原路径可接受的总延迟；
- note 写清楚接管范围，尤其要注明是否仍需原模型生成文本。

### 6.2 调用顺序

完整接管的标准形状：

```python
jev_value = await self._jev_noul(
    task="candidate_task",
    state=bounded_state,
    instructions="只描述一个清晰问题",
    true_hint="业务上的 true 含义",
    false_hint="业务上的 false 含义",
)
if jev_value is not None:
    return apply_jev_decision(jev_value)
return await existing_model_path()
```

单向前置过滤的标准形状：

```python
jev_value = await self._jev_noul(...)
if jev_value is False:
    return conservative_skip_result()
# True 表示“值得继续”，不是正文；None 表示不可用/不确定。
return await existing_judgement_and_generation_path()
```

不得使用 `if not jev_value`，因为这会把“明确否定”的 `False` 与“回落原路径”的 `None` 混为一谈。

### 6.3 状态文本

- 只发送完成该判断所需的最小上下文；
- 使用稳定字段名和明确边界，不把原消息中的指令当系统指令；
- 对用户文本、群聊历史、记忆摘要设置字符上限；
- 不发送 API Key、Authorization、完整内部配置、无关私聊或其他群内容；
- JEV 不负责生成 reason 时，生产日志使用固定的安全理由，不回显上游原始响应。

### 6.4 回退和生命周期

- `None` 永远表示执行原路径；
- 超时、熔断、低置信度、Key 缺失和任务未启用都必须无损回退；
- 配置重载、插件卸载时不得留下探针或判断任务；
- JEV 用量继续记入 `jev:systemone`，并遵守日硬限额；
- 新接线不得绕过原有安全检查、权限、频控、冷却或最终正文校验。

## 7. 校准与灰度验收

每个新场景建议按以下顺序上线：

1. **离线夹具**：覆盖明确正例、明确反例、边界例和 prompt injection 文本。
2. **影子期**：至少收集 200 个有效样本或连续 7 天，同时记录 JEV 与原模型结论，不改变行为。
3. **分歧复核**：分别检查假放行、假拦截，不只计算总一致率。
4. **默认关闭接线**：允许单任务 opt-in，保留原模型回退。
5. **小流量启用**：观察 p50/p95 延迟、低置信度回落率、熔断次数与用户纠正反馈。
6. **再评估默认值**：只有在收益稳定且误判代价可接受时，才讨论默认开启。

最低观测指标：

| 指标 | 说明 |
|---|---|
| JEV attempts / ok / failed | 判断调用量与可用率 |
| low-confidence fallback | 校准是否过于保守或问题描述不清 |
| model calls avoided | 真正省掉了多少原模型调用 |
| double-call rate | JEV 后又回落模型的比例；过高会同时增加成本和延迟 |
| p50 / p95 latency | 不能只看平均值，消息链路尤其关注尾延迟 |
| disagreement by direction | `send→drop` 与 `drop→send` 的风险不同 |
| user-visible correction | 用户追问、重复发送、手动重试、管理员恢复等间接误判信号 |

粗略收益判断应使用：

```text
净节省调用 = 被 JEV 直接结束的原模型调用数 - JEV 回落后产生的双调用成本
```

如果一个前置过滤器几乎总是得出“继续原模型”，即使准确率很高，也不构成有效优化。

## 8. 测试要求

新增任务至少覆盖：

- 任务注册、默认关闭、`runtime_wired` 守卫；
- JEV 正向、反向、`None`、超时、异常和低置信度回落；
- 原模型在 `None` 时确实只调用一次；
- JEV 已接管时原模型不再调用；
- 并发请求逐次记账，不重复计费；
- 审计、breaker、probe 错误不泄露 Key、Authorization、URL query/userinfo；
- 文本生成型场景验证 JEV 只做过滤，最终正文仍来自原模型；
- 高风险场景的默认行为不因升级改变。

自动化测试不得访问真实 JEV 端点。真实连通性使用 `scripts/astrbot_jev_smoke.py --probe` 显式执行，并记录这是一次会产生用量的外部调用。

## 9. 建议实施顺序

| 阶段 | 工作项 | 完成标准 |
|---|---|---|
| 1 | `group_question_wakeup_reply_review` 影子评估与接线 | 默认关闭；纯 `noul`；`None` 回落 120-token 原路径 |
| 2 | 建立每任务分歧统计 | 能区分 JEV、原模型、规则来源及两个方向的分歧 |
| 3 | `qzone_comment_reply_prefilter` 影子评估 | 只评估明确 skip；不生成正文、不绕过隐私检查 |
| 4 | `external_event_share_prefilter` 影子评估 | 本地强规则优先；JEV 只过滤明显无关信息 |
| 5 | 复盘两个已接线默认关闭任务 | 用真实延迟和误判数据决定是否继续 opt-in |
| 6 | 再决定主动人格任务 | 没有足够影子样本前不启用生产决策 |

当前不建议扩张 JEV 注册表来“占坑”。只有确定了输入、单一问题、回退路径、预算和校准方案后，才新增任务项。
