# Jev System One 判定委托

JEV 是 TypeSafe 的 System One 概率模型，用**一次请求同时回答多个"是/否、单选、打分"问题**，不生成文本。插件把它用于纯粹的判定类任务，替代原本每次都要消耗一次小模型调用的判断。

判断与生成的分界是本设计的核心约束：**JEV 只回答离散结论，任何需要产出自然语言的环节仍由原模型完成**。因此群聊插话只把"要不要插话"交给 JEV，插话正文仍由模型生成。

待接入场景的优先级、逐项风险、接线模板和灰度验收要求见 [JEV 优化机会清单与接入指南](./jev-optimization-opportunities.md)。

## 1. 为什么可以替代小模型

判定类任务原本用 `_llm_call` 发起一次 8–280 token 的调用，只为了拿一个布尔值或一个标签。JEV 直接返回概率，省掉生成开销，实测更快也更便宜。但**只有满足全部四个条件才适合迁移**：

| 条件 | 说明 |
|---|---|
| 答案是离散值 | 布尔、单标签或多标签打分；不能是文本 |
| 依据可放进文本 | JEV 不看图，视觉判定不可迁移 |
| 延迟预算 ≥ 0.4s | 实测单次 0.25–0.31s，需留抖动余量 |
| 误判代价可控 | JEV 没有"宁可错杀"的保守语义，高风险审核不迁移 |

按此标准，**线索词唤醒（`group_wakeup_context`）与出站重复检查不适合迁移**：前者原本是零成本正则，后者是哈希比对，交给 JEV 反而增加开销。线索词唤醒因此在注册表中默认关闭，需用户自行权衡开启。

## 2. 双端点与自动适配

两个端点协议相同，差异只在主机、密钥体系与模型命名空间：

| 密钥前缀 | 端点 | 模型 |
|---|---|---|
| `apikey_` | `https://api.typesafe.ai/v1/systemone` | `jev-latest` |
| `vck_` | `https://ai-gateway.vercel.sh/typesafe/v1/systemone` | `typesafe-ai/jev` |

端点族默认 `auto`，按密钥前缀识别，URL 与模型名各自取对应默认值。判定顺序为 **显式端点族 → 密钥前缀 → URL 主机 → TypeSafe 默认**。

URL 与凭据属于不同端点族时，**以凭据为准并忽略该 URL**，原因记录为 `api_key_family_over_gateway_url`。这处理升级场景：早期版本把 Vercel URL 作为 Schema 默认值下发，已有安装会把它持久化，与 TypeSafe 密钥并存时必然 401。也因此，`jev_gateway_url` 与 `jev_model` 的 Schema 默认值必须保持为空串——非空默认值会被当作显式覆盖，让自动识别失效。

## 3. 三种原语

线上实测确认的请求与应答形状：

| 原语 | 请求 | 应答 |
|---|---|---|
| `noul` | `{"type","instructions","criteria":{"true","false"}}`，criteria 可选 dict | `{"type":"noul","noul":0.95}` |
| `choice` | `{"type","instructions","criteria":{标签: 描述}}`，criteria 必填 dict | `{"type":"choice","choice":"a","confidence":0.97,"probabilities":{...}}` |
| `score` | `{"type","instructions","criteria":[低, 高]}`，criteria 必须两元素 list | `{"type":"score","score":0.13,"confidence":0.74,"legend":{...}}` |

三个原语可在同一次请求内混用，服务端并行且相互独立地评估。

`noul` 的 criteria 键必须使用字面 `true`/`false`。端点接受任意 dict 键而不报错，但会按语义解读：同一句话用规范键返回 0.29，改用 `the answer is yes`/`yes`/`1` 均返回约 0.59，只有文档规定的拼写校准正确。两个锚点要么都给、要么都不给，只给一半会让模型自行发明另一极。

`score` 返回 0–1，插件内部按 0–100 使用；其 `confidence` 在连续区间上天然偏低（实测 0.39），置信度下限不能照 `noul` 取值。

## 4. 调用链与延迟

`JevClient` 全程异步并复用 aiohttp 长连接。连接复用是能否用于低延迟判断的前提：

| 场景 | 耗时 |
|---|---|
| 每次新建连接（旧实现） | ~900ms |
| 复用长连接（中位数，8 次实测 251–305ms） | ~0.26s |
| 冷启动首次建连 | ~1.8s |

冷启动建连远超判定预算，因此插件启动与每次配置变更后执行一次 `warmup()` 把该成本移出消息链路。热身任务有唯一 owner，配置重载或插件卸载时会取消并等待清理；热身进行期间的新消息直接走原模型，不会让探测请求与第一条真实判定并发争抢冷连接。热身成功产生的 Token 也以 `jev_warmup` 进入账本。并发由信号量约束，运行时修改 `jev_max_concurrency` 会重建连接池和信号量。

旧实现用 `urllib` 在 `async def handle_group_message` 链路内同步直连，每次判定冻结事件循环一整个往返，这是必须改为异步的直接原因。

## 5. 任务注册表与回落语义

可委托的任务登记在 `JEV_TASKS`，每项声明原语、默认启停、运行时是否已接线、阈值、置信度下限与预算秒数。默认启用且已接线的四项是：`group_followup_judge`、`group_air_reply_guard`、`smart_silence`、`group_interject`。另外两项 `smart_message_debounce` 与 `rest_wakeup_judge` 已接线但默认关闭，需要明确加入 `jev_enabled_tasks` 才会运行。`jev_enabled_tasks` 留空（包括 Schema 下发的空列表）时使用四项默认值；非空但全是未知值时仍保持空集合，避免误开放委托面。

统一的回落契约是 **返回 `None` 表示"照原路径走"**：

- 闸门未启用、密钥缺失、任务不在启用列表
- 请求失败、超时、熔断中
- 概率未过阈值但置信度不足
- `choice` 返回了不在允许集合内的标签

判定为"否定"时返回的是 `False`，与"不可用"的 `None` 严格区分——两者都导致不回复，但原因不同，审计必须能分辨。

**已接线的判定点：**

| 任务 | 位置 | 原实现 | 接管范围 |
|---|---|---|---|
| 群聊沉默闸门 | `event_dispatch.py` | 8 token 调用，`REPLY`/`SILENCE` | 完整判定 |
| 群聊续接判断 | `event_dispatch.py` | 8 token 调用，`YES`/`NO` | 完整判定 |
| 智能沉默 | `user_memory.py` | 100 token 调用，JSON | 完整判定 |
| 群聊主动插话 | `group_observation.py` | 140 token 调用 | **仅前置过滤** |
| 智能消息收口（默认关） | `event_dispatch.py` | 80 token 调用，JSON | 本地快判后的模糊区完整接管 |
| 休息醒来判断（默认关） | `main.py` | 180 token 调用，JSON | `rest_reply_mode=llm` 时完整打分 |

前三项决策与输出本就分离，JEV 可完整接管。插话不同：**决策与正文由同一次 140 token 调用产出**，无法只接管决策。因此改为前置过滤——JEV 判"此刻不适合插话"时直接返回，省掉整次调用；判定适合或不可用时照原路径继续，由模型产出正文。这样只会减少调用，不会让插话判断变差。

续接判断的 JEV 调用位于提供商检查**之前**，因此未配置判定模型的群也能受益——原实现在无模型时直接返回 `None` 走规则，属常见配置。

其余四项 `group_member_safety`、`group_wakeup_context`、`proactive_persona_judge`、`emotion_judgement` 仍只保留为未来接线的注册项。闸门会对这些未接线任务 fail closed：即使误填进配置也不会发起网络请求；诊断中的 `configured_unwired_tasks` 会明确列出它们。

## 6. 熔断、审计与记账

连续失败达到 `jev_fail_threshold` 后暂停调用 `jev_fail_open_seconds`，避免 JEV 故障时每条消息都白等一个超时。

每次尝试记录任务、判定、概率、阈值、耗时、用量与回落原因。审计中 `decision` 记录 JEV 的原始回答，`reason` 记录闸门为何覆盖它，两者在审计文本中必须可区分——早期版本只写"低置信度回落"，与 `decision=reply` 并列时读起来自相矛盾，现改为写明具体数值。总览接口还暴露安全裁剪后的 JEV 诊断（不含 Key、URL 用户信息、查询串或片段），包括 `warmup_attempted` 与 `last_probe` 的状态、来源、时间、耗时、HTTP 状态和脱敏错误。

Token 用量并入插件既有账本，`provider_id` 为 `jev:systemone`，受日硬限额约束；`jev_count_toward_token_limit` 关闭时不参与限额但仍记账。记账直接消费每个 `JevResult` 的用量，不再用全局累计值做前后差，因此并发判定不会把另一请求重复计费。

显式连通性探针为 `POST /api/v1/plugins/extensions/astrbot_plugin_private_companion/page/jev/probe`。它会复用正在进行的启动热身，且在总开关关闭但 Key 已配置时仍可执行；达到 Token 日硬限额时返回 `skipped_budget`。探针会产生一次低成本 JEV 请求，其用量记为 `jev_manual_probe`，响应不包含答案、Key 或原始上游响应。

仓库内的 `scripts/astrbot_jev_smoke.py` 默认只读 `/api/v1/plugins` 与 `/page/overview`。通过环境变量 `ASTRBOT_BASE_URL`、`ASTRBOT_API_KEY` 提供连接信息；只有显式传入 `--probe` 才会调用上述付费探针。脚本不会打印 Key 或完整接口响应，并会对明文 HTTP 给出警告。

## 7. 后续候选场景

| 优先级 | 场景 | 结论 |
|---|---|---|
| 高 | `group_question_wakeup_reply_review` | 当前 120 token、只输出 `send/drop`，适合下一批新增 `noul` 任务并先做线上校准 |
| 中低 | `proactive_persona_judge` | `send/defer/drop` 外还可能要求改写，JEV 只能做前置过滤，不能完全替代 |
| 不迁移 | `group_member_safety` | 误判代价高，且仍需 reason/severity |
| 不迁移 | `emotion_judgement` | 需要 event/target/intensity/severity/tier 多字段 |
| 不迁移 | `group_wakeup_context` | 当前是零成本正则，迁移只会增加调用与费用 |
| 不迁移 | 视觉、文本生成、密码、语音/图片 prompt | 不是纯文本离散判断，超出 JEV 能力边界 |

## 8. 配置项

`basic_config` 章节下 12 项：`enable_jev_decision`（总开关，默认关）、`jev_api_key`、`jev_endpoint_kind`、`jev_gateway_url`（留空自动）、`jev_model`（留空自动）、`jev_timeout_seconds`、`jev_max_concurrency`、`jev_enabled_tasks`、`jev_threshold_overrides`、`jev_fail_threshold`、`jev_fail_open_seconds`、`jev_count_toward_token_limit`。

配置必须经 `_initialize_jev_config` 落到实例属性：判定点通过 `_persona_value`/`persona_setting` 读取，而该解析器读的是实例属性而非配置映射。早期集成缺少这一步，`jev_api_key` 恒为空字符串，导致功能从未真正执行且失败路径静默。

## 9. 模块与测试

| 文件 | 职责 |
|---|---|
| `domains/decision/jev_endpoint.py` | 端点画像、自动适配、凭据冲突提示 |
| `domains/decision/jev_primitives.py` | 三原语构造与容错解析 |
| `domains/decision/jev_engine.py` | 异步客户端、连接复用、预热、可诊断错误 |
| `domains/decision/jev_tasks.py` | 任务注册表与启用集合归一化 |
| `domains/decision/jev_gate.py` | 启停、阈值、熔断、审计 |
| `jev_decision.py` | 插件侧接线：配置读取、探针、记账、诊断 |

JEV 自动化测试全部**离线运行**，不依赖 AstrBot 线上实例、不访问网络、不消耗额度：闸门测试注入脚本化假客户端，端点解析为纯函数。覆盖端点适配、三种原语、闸门与熔断、热身/手动探针生命周期、并发复用与精确记账、错误脱敏、未接线任务 fail closed、页面路由以及生产调用点的回落守卫。

需真实端点的联调不在该文件内，避免 CI 依赖网络与密钥。
