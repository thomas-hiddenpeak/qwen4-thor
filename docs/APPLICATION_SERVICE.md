# 应用服务层与 OpenAI 兼容矩阵

### Responses 图像上下文

通过 Responses 的 `image.context_compression` 可选择图像输入策略。字段按请求生效，取回已有回答时会返回原策略；compact 会保留客户提交的原图与 detail。[业务回执](evidence/serve-image-speech-recovery-20261126/delivery.json)

### 语音合成响应的媒体类型

rev522 的 `POST /v1/audio/speech` 由请求体的 `response_format` 选择编码。二进制响应按 `Accept` 选择媒体类型。官方 SDK 默认发送的 `application/octet-stream` 返回文件字节。对 Opus，显式 `audio/opus` 和通配请求采用相同的 Ogg Opus 文件封装，通配请求的响应头为 `audio/ogg`。客户端指定 `stream_format: "sse"` 时，音频字节经 `speech.audio.delta` 返回，最后收到 `speech.audio.done`。四种响应形式均在原生 TLS 入口实测。[发行及恢复回执](evidence/serve-image-speech-recovery-20261126/delivery.json)


q4t 在单台 Thor 上提供原生 LLM 推理和应用服务。总计划覆盖官方接口目录中的 157 条推理与应用路径，共 231 个必做 HTTP 方法。组织管理、云账单和微调训练由范围决定排除。逐方法对照区分接口声明和真实业务验收。[范围决定](evidence/serve-scope-decision-20261120/README.md) · [实际进展和发行身份](STATUS.md)

| 开发接入 | 已验证的业务 | 客户端和部署条件 |
|---|---|---|
| Assistants SDK 轮询与分页（rev494） | 单客户只有一个生成名额时，可在运行中读取状态、消息和步骤。四类列表的非空游标取自对应对象 ID，空页返回空字符串。 | 容量切换、模型退出和独立回退包接管后，原客户端均能读取同一 Run 的历史。跨客户读取返回 404。[发行回执](evidence/serve-assistant-pagination-20261125/delivery.json) |
| Chat 语音与审核响应（rev494） | 发行 OpenAPI 覆盖音频输出、通过音频 ID 续聊，以及内容过滤结束原因和审核流式块。 | 已用原始 HTTP 与 SSE 记录核对官方和发行 schema。[语音记录](evidence/serve-chat-audio-wire-20261125/wire.json) · [审核记录](evidence/serve-chat-audio-wire-20261125/moderation.json) |
| Evals 余弦评分（rev482） | 选择 `text_similarity` 和 `evaluation_metric: "cosine"` 后，运行会用同一客户的嵌入配额计算候选文本与参考文本的相似度。分数为 0 到 1，每次评分的 token 用量记入 `per_model_usage`。 | 通过 `--embedding-model-dir` 配置 Qwen3-Embedding-0.6B，在 `/v1/capabilities` 查看启用状态。使用本地标注样本校准通过阈值。OpenAI 托管评分使用 text-embedding-3-large，两种模型的得分可能不同。[OpenAI 评分文档](https://developers.openai.com/api/docs/guides/graders) |
| Evals 英文 METEOR | rev488 已发行 | 按词对齐、词干和 WordNet 同义词计算分数；复用隔离评分进程、客户取消、截止时间和逐行工作预算。词库随包分发。云端的分词与词库版本未公开，该实现已与 NLTK 3.9.2 做差分。[发行回执](evidence/serve-evals-20261125/meteor-delivery.json) |
| Evals 运行报告（rev482） | 从创建、读取、列表或取消响应的 `report_url` 获取 HTML，可查看运行状态、成败计数和各评分项结果。使用同一项目的认证头访问。报告只展示前 100 项评分，完整数据仍可从页面中的两个 JSON 链接读取。 | 原生前置按已验证的 Host、协议和部署前缀生成绝对地址。匹配客户端策略的可选 nginx 前置转发外部地址。旧式匿名上游或固定外部地址的部署可在模型进程设置 `Q4T_EVAL_PUBLIC_BASE=https://llm.example/tenant`。地址只含协议、主机、可选端口与路径前缀，末尾省略斜杠。 |
| Chat 工具历史回放（rev502） | 后续请求省略工具声明时，保留历史调用中空值、布尔值、数字、数组、对象与同形字符串的区别。 | 真实资料查询中的空值和字符串经模型重启与网关换包后，保留原始参数和调用 ID。JSON 和 SSE 均返回正确类型以及 157 条资料路径。[发行回执](evidence/serve-history-argument-types-20261125/delivery.json) |
| Evals 运行取消（rev496） | 取消进入独立名额，可从繁忙客户的普通状态查询中独立准入。 | 真实 SDK、普通读槽占满、取消并发上限与恢复后的租户隔离均通过。[取消验收](evidence/serve-observation-admission-20261125/business.json) |
| 视频角色引用 | SDK 可上传命名参考片段，并按 ID 读取所属客户的元数据。保留原始编码供后续视频任务取用。 | rev498 已验证上传、按 ID 工具查询、租户隔离与损坏输入。监督器重启模型及独立回退包接管后，原角色均可读回。rev500 还支持仅启用一条视频轨的 MP4；两种容量按选中轨读取方向与时长，整份上传原片保留。视频生成待验收。[双轨与 SDK 恢复](evidence/serve-video-track-selection-20261125/delivery.json) |
| ChatKit 语音输入 | 浏览器使用有效的会话密钥向 `/v1/chatkit` 发送 `input.transcribe`，得到 `{"text":"…"}`。部署须配置原生 ASR 模型。 | rev496 已用 WAV、WebM/Opus 验证中文录音、错误处理和接管后的转写。[发行回执](evidence/serve-chatkit-composer-20261125/delivery.json) |
| ChatKit 消息反馈和实体提及 | rev490 已发行。浏览器保留评价与标签，管理 SDK 显示提及文本。 | 两份公开 schema 与 49 项配置迁移检查已通过。模型、持久化和恢复验收见[记录](evidence/serve-chatkit-20261125/feedback.json)。 |
| ChatKit 文档两步上传 | 浏览器调用 `attachments.create`，随后按返回地址、凭证和媒体类型 PUT 原始字节。完成前无法引用文件。有效期为 5 分钟或剩余会话时间中的较短者。 | 原生前置采用外部 Host、协议和部署前缀。自定义公开地址可设置 `Q4T_CHATKIT_PUBLIC_BASE`。原生 TLS 实机已用该文档完成两轮问答，rev492 验收了配置切换、模型重启与回退接管。[验证记录](evidence/serve-chatkit-20261125/upload.json) |
| ChatKit 图片附件 | 候选支持视觉模型按文件 ID 读取图片及源文件删除后的重试。直接上传 PNG、两阶段上传 JPEG 已核对真实像素、模型答案与快照；短验记录有间歇拒答。 | 视觉配置使用 `--allow-media --no-mtp`。浏览器仍以普通文件显示，图片预览待实现。[业务回执](evidence/serve-chatkit-image-attachments-20261126/business.json) |
| ChatKit 文档直传 | 浏览器按会话配置分段上传文件，模型读取真实文件，同一线程可在删除上传源后续谈。 | 已验收权限、容量、附件删除和标准历史返回。rev478 已验收部署接管后的原附件与工具回填。两阶段文档上传见上一行。图片预览待实现。[业务回执](evidence/serve-chatkit-20261125/attachments.json) |
| ChatKit 会话与线程（rev478） | 官方 SDK 六项会话和线程管理操作；浏览器消息与函数结果回填、流式回答和历史分页。 | 同项目按用户、工作流及历史策略过滤。主包故障后，重新签发的用户会话可以接续原线程、附件及待回填调用。[发行回执](evidence/serve-chatkit-20261125/delivery.json) |
| Webhook | rev472 提供八项管理方法及后台 Responses、Batch 终态通知，独立 HTTPS 发送器验签、重试并可跨进程接手。 | 需部署方显式配置目的地。订阅方按稳定事件 ID 去重。默认部署关闭出站，订阅和待投递记录可以跨故障接手。[发行回执](evidence/serve-webhooks-20261124/delivery.json)。[实测回执](evidence/serve-webhooks-20261124/lifecycle.json) |
| Agents 托管工作区 | rev474 支持共享工作区、空闲挂起和检查点恢复。rev476 增加含至少两项 Files 引用的异步预热，准备期间可接入会话。 | 客户可订阅就绪或失败事件。创建成功后可删除原文件；准备任务按客户、任务轮转，源文件固定最长一小时。模型退出与独立回退后均恢复了原工作区和待回填调用，两个上传源均已删除。[发行回执](evidence/serve-webhooks-20261124/preparation-delivery.json) |
| Chat、Responses、Agents | 项目资料查询、流式工具事件、工具结果回填、子 Agent 回合及配额恢复 | API Key 区分客户。持久会话跨进程恢复；具体模型、工具及发行组合见[托管业务回执](evidence/serve-agent-workspaces-20261124/subagent-delivery.json)。 |
| Agents 定时任务 | 通过官方 SDK 管理、手动触发或定时投递任务，查询每次投递。模型可在启用 `automations` 的会话内安排唤醒。 | 当前回合等待客户端工具时，唤醒排队。rev470 验证了工具回填和到期投递跨主轻配置、模型退出及网关换包后的恢复。[发行回执](evidence/serve-agent-automations-20261124/delivery.json) |
| Assistants v2 | 注册及更新 Assistant，维护 Thread 和 Message，流式创建 Run，按工具调用 ID 回填，读取步骤、取消及恢复待回填的 Run。附件检索已读取真实发布记录。 | 使用客户 API Key 和 `OpenAI-Beta: assistants=v2`。仅可访问本客户的数据。工具回填及崩溃接手见[发行回执](evidence/serve-assistants-20261124/delivery.json)，跨轮输入预算与恢复见[rev464 发行回执](evidence/serve-skills-20261124/delivery.json)。托管代码执行见下行 rev466 回执。 |
| Code Interpreter 工作区 | Responses 和 Assistants 以 Python 读取授权项目文件、写出可下载文件，流式代码、日志与引用可读回。 | 系统 CPython 3.12 仅在隔离子进程加载；持久化文件，预载标准库。主、轻配置和故障恢复均已按原 ID 接手文件。[rev466 发行回执](evidence/serve-code-interpreter-20261124/delivery.json)。 [业务回执](evidence/serve-code-interpreter-20261124/business.json) |
| Skills 与版本 | 创建、列举、更新和删除技能，保留不可变版本，按 ZIP 或 Base64 下载，工作区可绑定固定版本。主、轻配置和进程恢复已验证版本内容。 | 原生 TLS 和 API Key 隔离客户。服务通过隔离文档工作进程读取包中的 YAML。模型执行技能仍在开发。[发行回执](evidence/serve-skills-20261124/delivery.json) |
| Containers 与容器文件 | 官方 SDK 创建和删除工作区，流式上传和下载文件，从 Files 复制发布记录；源文件删除、模型更换及回退后均可读回原内容。 | 原生 API Key 按客户隔离，使用固定的技能快照。Code Interpreter 使用隔离子进程，禁用网络，保留输出文件。工作区上限为 16 MiB，单文件上限为 8 MiB，共 256 个文件。[发行回执](evidence/serve-skills-20261124/delivery.json) |
| 实时语音翻译 | 独立短期凭据、WebSocket 与浏览器 WebRTC。可选源字幕、目标字幕；WebRTC 以真实远端音轨交付译音。 | Chromium 148 与官方 Node SDK 已实测，跨凭据 401，低额度关闭可见。rev468 主轻配置与整包恢复已验。浏览器媒体轨需要安装可选媒体包及其 Ubuntu 依赖。[恢复回执](evidence/serve-realtime-translation-20261124/delivery.json)。 | [WebSocket](evidence/serve-realtime-translation-20261124/business.json)、[WebRTC](evidence/serve-realtime-translation-20261124/browser.json) |
| 旧版 Realtime 对话与转写 | Node SDK 7.47.0 在原生 TLS 上完成中文转写、文本增量及终态；两种容量均已验证 | SDK WebSocket 助手使用 WSS，部署时为其配置原生 TLS 或可选 TLS 网关。转写须通过支持 transcription intent 的连接助手并显式传入 beta 头。[SDK 回执](evidence/serve-realtime-legacy-sessions-20261124/beta-resources.json) |
| 上传语音翻译 | 中文真实录音译为英文，JSON、纯文本、详尽 JSON、SRT 和 VTT 均已通过两种容量短验 | 配置原生 ASR；字幕还需对齐模型。`whisper-1` 是请求别名，实际后端为 Qwen ASR 和本地 Chat。[双配置回执](evidence/serve-audio-translation-20261124/profiles.json) |

#rev520 支持 Agent 会话的 JSON Schema 输出。客户端在 `agent.text.format` 提交 `type` 和 `schema`。内部 Responses 调用补入名称并启用严格约束，查询会话时返回客户提交的配置。缓存寿命更新应用于后续回合，已失败的模型输出保留在其原回合。

## Python 读取已保存的 Chat 输入

在仓库内使用官方 Python SDK 时，可用下面的方法按发布的应用 schema 校验每一页。Python SDK 的严格响应类型把已保存输入的角色限定为 assistant；实际记录也包含用户、工具及指令角色。这个辅助函数使用 SDK 现有的鉴权、传输和分页参数，返回通过校验的原始字段。运行示例的客户端环境需要 `jsonschema`。

```python
from tools.serve_chat_storage.python_client import (
    list_stored_messages, stored_messages_validator,
)

validator = stored_messages_validator()
page = list_stored_messages(client, completion_id, validator=validator,
                            order="asc", limit=20)
while True:
    for message in page["data"]:
        print(message["id"], message["role"])
    if not page["has_more"]:
        break
    page = list_stored_messages(client, completion_id, validator=validator,
                                order="asc", limit=20, after=page["last_id"])
```

rev504 按公开字段 `refusal` 返回拒答消息。调用方原先提供拒答数组时，读取结果在 `original_content_parts` 中保留原数组；若输入还提供了独立的 `refusal` 字段，该值也予以保留。[发行回执](evidence/serve-stored-message-projection-20261125/delivery.json)

## 2026-11-08 文本与视觉 v1 验收范围

当时验收了文本 Chat、只读项目资料查询、工具调用、结构化输出、错误与取消，以及分阶段的运行保障与任务恢复。

视觉v1新增显式单槽no-MTP图片/工具/JSON及媒体展开配额组合；
视频方向任务、硬解码未接受，旧托管/持久任务不自动迁移。
详见[视觉矩阵](VISION_SERVICE_V1.md)及[独立结果](VISION_PRODUCTION_RESULT_2026-11-07.md)。

当前发布身份和下一项工作只在[STATUS](STATUS.md)维护；本页维护能力与
边界。原逐阶段叙述移入[历史快照](APPLICATION_SERVICE_HISTORY_2026-11-04.md)，
原结果、失败、证据和日志均保留。[SRV-20合同复核](SERVE_CONTRACT_REAUDIT_2026-11-04.md)
只核对源码与证据；后续[03c锁等待取消](SERVE_PREPARE_LOCK_RESULT_2026-11-04.md)
已新增原生/真实工具短验并交付默认。

该阶段的默认服务封装已完成[原生入口及有界自恢复](SERVE_NATIVE_INGRESS_RESULT_2026-11-08.md)：
Bearer鉴权、模型/控制器/入口恢复无需nginx或systemd；Python控制面
仍保留。151项不同CPU及3启动/18Chat真实短验通过，当时登记的服务意图为 STOPPED。旧客户端、托管与持久任务仍按各自已验身份使用，
不因新服务入口交付而自动完成协议迁移或视觉恢复组合验收。

## 边界与成熟度

模型计算正确、模板一致、响应可解析、客户端可接入和工具业务正确是
五个不同层次；一层通过不能替代另一层。近期接受范围由
[12k原生监听](SERVE_STARTUP_LISTENERS_RESULT_2026-11-04.md)、
[12l托管截止与停止](SERVE_SUPERVISOR_DEADLINES_RESULT_2026-11-04.md)和
[11k当前协议任务交接](SERVE_TASK_STARTUP_PROTOCOL_RESULT_2026-11-04.md)
分别证明。12l保留首轮监控归属错误的ENV_ABORT，采用通过业务段与定向
停止补验，不能合成为一次连续全流程成功。
固定协议cc321239的[托管组合](SERVE_CURRENT_MANAGED_RESULT_2026-11-05.md)
与[持久任务交接](SERVE_CURRENT_TASK_RESULT_2026-11-05.md)已独立短验；
旧15d7ccb1包保持，任务按已登记方向显式迁移，不会自动升级。
[可选部署profile](SERVE_BOUNDED_DEPLOYMENT_RESULT_2026-11-05.md)已有离线
检查入口；当前ELF+host域4响应/2工具通过，但压力在安全余量处停止，
活跃/排队的内存退场组合未接受。c1653470候选不替代已验41dc8fdb。

“限定已验”仅指链接结果的源码身份、运行配置、客户端版本和用例范围。
当前托管包的短验配置为单槽、8192上下文、2048预填充及独立运维端口；
不能把它扩张成208896上下文、多槽或所有可选项同时开启的组合验收。
历史证据需按依赖变化判断可复用性；版本摘要一致也不等于语义完备证明。

## 运行方式与可选能力

用户明确高级功能在执行时选择启用。基础直连开发不要求先提供LAN、
证书、来源网段或API Key；启用某项时才核对其配置、依赖和实际部署。
已有内存准入等基础保护保持默认行为，不把“可选高级部署”理解为关闭
既有保护。准备包不等于启动、安装或验收。

| 运行方式 / 能力 | 启用边界 | 已有证据与限制 |
|---|---|---|
| 本机文本Chat | 原生serve；默认loopback，不要求TLS材料 | JSON/SSE、工具与错误合同；[03c当前默认短验](SERVE_PREPARE_LOCK_RESULT_2026-11-04.md) |
| 独立运维入口 | 显式`--operations-port`，默认0；仅loopback | 公共慢头满额下运维可用；自身控制/观测仍各4额 |
| 独立Key、来源限制与可选TLS | 原生service入口自行验证Bearer/Host/实际CIDR；runner绑定client策略；TLS可由外部HTTP网关提供 | [原生入口短验](SERVE_NATIVE_INGRESS_RESULT_2026-11-08.md)；实际LAN/DNS/证书/防火墙另验，不强制nginx |
| 服务托管 / 重启 | 原生serve不隐式启动Guardian；显式新service包具有持久预算与有界恢复，旧托管包按原合同运行 | [模型/控制器/入口恢复](SERVE_NATIVE_INGRESS_RESULT_2026-11-08.md)已验；不重放旧请求，Guardian整树死亡需外层启动 |
| 部署组合/回退检查 | 显式profile prepare/check，固定包摘要 | [工具与有限结果](SERVE_BOUNDED_DEPLOYMENT_RESULT_2026-11-05.md)；只检查身份与组合，不授予业务接受；内存退场组合NO_GO |
| host记账域 | 显式host memory high/max及tasks max配置 | [02k](SERVE_HOST_ENVELOPE_RESULT_2026-11-04.md)；不覆盖全部CUDA物理RAM记账 |
| CUDA申请额度 | 原生默认auto，可显式调整`--cuda-allocation-limit-mib` | [02j2生产接线](SERVE_CUDA_QUOTA_INTEGRATION_RESULT_2026-11-04.md)；申请额度不是整机物理RAM硬限额 |
| 告警、通知与私有留存 | 显式观察器、接收器、目的地及保留策略 | [12c](SERVE_NOTIFY_RESULT_2026-11-04.md)、[12e](SERVE_RETENTION_RESULT_2026-11-04.md)等本机短验；正式渠道按需验收 |
| 持久项目任务 | 独立应用入口和显式TLS连接profile；原生Chat本身不持久化业务 | [当前交接](SERVE_CURRENT_TASK_RESULT_2026-11-05.md)：已存工具结果复用、永久保键、显式兼容迁移；UNKNOWN不自动重放 |

## 项目安全记录

具备此能力的版本提供 `GET /v1/safety/alerts/{id}` 和
`GET /v1/safety/cases/{id}`。客户通过自己的 API Key 读取已审核的
告警和案例。订阅 `safety.alert.created`、`safety.warning_issued` 或
`safety.deactivation_issued` 后，通知中的 `data.id` 可用于查询。
同一记录 ID 对其他客户返回 404。

操作员在服务主机以 q4t 的运行身份提交已审核记录。对象目录选择具体服务，
`--client-id` 指定已配置的客户。输入 JSON 包含 `resource`，字段与上面的
公开响应一致。告警中的 `request_paused` 应取自实际的阻断登记结果。
案例的 `notice.type` 记录已出具的警告或停用通知。这条命令不会发起停用操作。

```bash
q4t safety publish --object-store-dir /srv/q4t/objects --client-id alpha < approved.json
```

同一个客户重复提交相同的已审核记录会得到相同回执。改变内容并沿用原 ID
会返回冲突。默认保留期从 `created_at` 起算 30 天，也可在输入外层用
`record_expires_at` 指定 Unix 秒，受对象库的保留上限约束。零数据保留告警
的 `reason` 为 null。若授权提供临时说明，同时提交
`resource.detailed_explanation` 和 `explanation_expires_at`，服务将说明存于
单独到期的对象中。说明到期后仍可读取在保留期内的告警摘要。

## API 兼容矩阵

托管模型替换与独立网关回退均已验证，客户用新会话能够接手原工具调用。[rev514 发行回执](evidence/serve-chatkit-reasoning-workflow-20261126/delivery.json)

跨配置验证覆盖读回真实历史摘要、接手续签会话中的工具调用，以及两种身份隔离。主轻配置都从浏览器和管理 SDK 读到相同的公开任务，轻配置用原调用继续回答发行号 512。[验收记录](evidence/serve-chatkit-reasoning-workflow-20261126/profiles.json)

rev514 通过浏览器的 workflow 和管理 SDK 的 chatkit.task_group 提供同一份公开摘要。流式更新、终态和线程历史使用同一条目 ID。摘要步骤拒答时保留原业务答案，摘要可以为空。定向短验中，模型新生成了 1 份公开摘要。[业务回执](evidence/serve-chatkit-reasoning-workflow-20261126/business.json)

ChatKit 的管理 SDK 与浏览器 POST 共用存储。rev506 已验证 21 条消息分页，也能从原用户消息重试，保留此前上下文及附件快照，通过原线程提交新生成的工具结果。rev508 首次实现固定参考的全部 16 个浏览器操作名。新增结构化输入回填、流式和同步自定义动作，已验证同一工作流在模型和网关故障后继续。[rev508 发行回执](evidence/serve-chatkit-actions-20261125/delivery.json) [分页回执](evidence/serve-chatkit-page-defaults-20261125/business.json) [重试回执](evidence/serve-chatkit-retry-20261125/business.json) [恢复与发行](evidence/serve-chatkit-retry-20261125/delivery.json)

工作流可用 Agent metadata 的 `q4t_chatkit_structured_input_tool` 指定一个已注册函数。当该函数返回含 `inputs` 的有效提问参数时，浏览器收到带稳定 ID 的 `structured_input`，可用 `threads.add_structured_input` 回填选择或自由文本。管理 SDK 可按同一 ID 查看原函数调用及结果。历史工作流继续使用普通工具输出。该能力已在 rev508 发行。[业务回执](evidence/serve-chatkit-structured-20261125/business.json)

工作流可通过项目管理凭据上传 JSON 动作文件，并将其 Files ID 登记为 Agent metadata 的 `q4t_chatkit_actions_file`。文件中的 `actions` 是动作名到 JavaScript 源码的映射。签发 ChatKit 会话时按项目权限读取并固定代码；删除源文件后，已签发会话可继续使用该版本，新会话须有可读的部署文件。普通模型请求沿用原工作流。

动作名由浏览器提交。处理程序在项目已有的隔离 JavaScript 工作者内运行，可以从 `actionContext` 读取 `action`、`sender`、`thread`、`state`、`state_variables` 和 `streaming`。例如，登记为 `show_release` 的处理程序可使用下列代码：

```js
const value = String(actionContext.state_variables.revision);
text({widget: {type: "Card", children: [{type: "Text", value}]}});
```

工作流调用一次 `text` 提交状态和组件。同步动作返回 `updated_item` 或空值；流式动作可在工作者内调用 `progress("读取发行记录")`，浏览器收到进度后可以断开请求。每次动作最多发送 64 次实时进度，每次最多 4096 个 UTF-8 字节。结果中的 `progress` 和 `effects` 仍按原协议在提交后发出。结果可以附带 `notices`、字符串或空的 `title`，流式动作还可用 `remove_sender: true` 移除触发动作的组件。线程标题、组件和状态在同一修订上原子提交。写入冲突或无效结果在发出首条进度前返回 HTTP 错误，之后发出终止错误事件；此前已送达的进度不代表写入成功。默认 rev510 已交付上述行为。10 个 JSON 操作、6 个 SSE 操作有分操作输出合同；业务回放覆盖 12 种事件，其他线程条目及嵌套更新形态仍需验收。[发行回执](evidence/serve-chatkit-browser-output-20261125/delivery.json)[动作与合同验收](evidence/serve-chatkit-browser-output-20261125/business.json)

rev518 为流式动作处理程序提供 `streamWidget(root)`。每次传入同一条目的完整快照。有发送者时更新原条目，新组件的 ID 由服务端生成。为文本组件设置稳定的 `id` 后，客户端可以按增量显示后续字符。下面的处理程序先显示“发行”，再补上从工作流状态读取的版本号。

```js
const card = value => ({type: "Card", children: [
  {type: "Text", id: "release", value}
]});
streamWidget(card("发行"));
streamWidget(card("发行 " + actionContext.state_variables.revision));
```

要替换单个后代组件，调用 `streamWidgetComponent("release", {type: "Text", id: "release", value: "复核通过"})`。目标取自当前动作的快照或其发送者。浏览器收到 `widget.component.updated`，事件中的 `component_id` 标明旧位置。新组件可以改用另一个 ID 或省略 ID；后续动作按更新后的树查找目标。组件 ID 冲突、目标不存在或合并后的树超过读取预算都会终止动作。

只更新组件时，处理程序正常返回即可。还需修改状态时，在结尾调用一次 `text({state: ...})`。工作者成功退出并提交线程修订后，浏览器会收到新条目的 `thread.item.done` 或已有条目的 `thread.item.replaced`。在此之前看到的快照是临时结果，若收到错误或连接中断，可重新读取线程来恢复已提交内容。`progress` 和 `streamWidget` 共用 64 帧及 512 KiB 的工作者消息预算，单个组件快照最多 64 KiB，每条进度文本最多 4096 个 UTF-8 字节。

模型替换及网关换包后均从原附件答出发行号 510。[rev512 发行回执](evidence/serve-chatkit-generation-stream-20261125/delivery.json)

真实主轻配置验证了中断回答和资料附件随原线程恢复，下一次提问得到原资料中的数值。两类越权读回均返回 404。[跨配置回执](evidence/serve-chatkit-generation-stream-20261125/profiles.json)

默认 rev512 的普通生成流公布 `stream_options.allow_cancel`。同一请求断开后，原作业结算为 `cancelled`，线程保存服务端停止时已生成的非空助手文本。续聊时模型收到一条隐藏中断说明。显式后台 Responses 的观察连接仍可断开重连以继续读取同一作业。[断开验收回执](evidence/serve-chatkit-generation-stream-20261125/cancellation.json)

默认 rev512 将来源编号按消息和内容段分别计数，同时保留 Unicode 字符位置。注册工作流的网页搜索沿用 Agent 的域名、位置、上下文大小和联网模式；浏览器可指定已注册的 web_search。已验证引用在 SSE、历史页和管理 SDK 中的各自格式。普通生成流断开后的部分消息恢复仍待交付。[引用与错误终态](evidence/serve-chatkit-generation-stream-20261125/business.json)

本项目支持的完整输入见[38字段schema](serve-chat-request-v1.schema.json)
与[必需原生语义](serve-input-semantics-v1.json)；10条路由见
[OpenAPI](serve-openapi-v1.json)。下表按调用者可见行为分组，不以静态
schema代替动态预算、模板、语法编译或真实业务验收。
10条是后端API清单；当前原生公共入口只转发8条明确路由，不公开
`/metrics`和`/v1/config`，两者保留在本机后端/运维入口。入口的
`/livez`表示自身存活；`/readyz`还必须满足恢复代次自检及配额隔离。

| API / 能力 | 当前状态 | 限制与证据 / 行动 |
|---|---|---|
| POST /v1/chat/completions | 已验，子集 | 单模型；n=1..128独立choice，串行执行、同一请求生命周期 |
| GET /v1/models | 已验，SDK 3.42.1 | 单个配置模型；不支持模型详情 / 管理 |
| GET /v1/capabilities | 已验，Q4T 扩展 | 实现身份，不是发布或质量证明 |
| messages | v3消息子集已验 | 多条/中途system与developer按序映射system ChatML；user/assistant/tool与ID关联保持；无需user；refusal文本/内容块可回填。见[实际SDK交付](SERVE_CHAT_INPUT_2026-11-08.md)，剩余legacy/custom/多模态合同继续实现 |
| message.name | 已实现并短验 | JSON转义的参与者信息，4096 UTF-8字节上限；按名称取回用户信息已通过真实SDK；不作鉴权身份，tool_call_id负责工具关联 |
| prompt / parse_response=false | 已验，Q4T 扩展 | raw 文本；不解析工具或推理 |
| JSON / stream=true | 限定已验 | 首帧提前；5秒注释心跳；finish → 可选 usage → DONE；中断不能当完整响应 |
| content / reasoning_content / reasoning | 05a限定已验 | 默认reasoning_content，可用reasoning_field选择；JSON/SSE固定单字段；历史别名相等合并/冲突拒绝 |
| include_reasoning | 05a限定已验 | bool默认true；false隐藏解析出的推理，计算/usage保留；raw/unparsed与null拒绝，不是正文过滤或隐私隔离 |
| thinking_token_budget | 05b限定已验 | parsed messages精确整数0..2147483647/null；自然配额+最多14强制边界token，总输出优先；MTP预留pending后回退；不是FLOPs上限 |
| function tools / tool_call_id | 已验 | ID 唯一完整批次；文本结果可乱序归一；客户端执行 |
| tool_choice | 已验，子集组合 | auto / none / required / named；named 恰好一个调用，显式约束模式仅 parsed messages |
| 历史函数参数回填 | rev486 已发行 | 带 ID 的历史调用及旧式 function_call 可保留被工具拒绝的参数原文；后续轮次可按校验错误重新生成调用。 |
| parallel_tool_calls | 已验 | false 最多一个；true 允许多调用，不代表应用并发执行 |
| allowed_tools / 工具参数SSE | A04子组已验 | auto/required允许子集贯穿模板、grammar与最终校验；严格/多调用逐片参数，成功终态＋EOF前不得执行。见[实际资料短验](SERVE_TOOL_STREAM_2026-11-08.md)；legacy/custom继续实施 |
| strict / JSON Schema 输出约束 | 有限 profile 已验 | q4t-json-schema-v1，逐 token 掩码＋最终验证；未知关键字拒绝，应用仍须验证业务 |
| max_tokens | 已验 | 正整数；包含推理 / 模板生成 token；受上下文容量约束 |
| max_completion_tokens | 已验 | 与 max_tokens 共用生成预算；两个字段同时非空时拒绝歧义 |
| temperature / top_p / n | A03子组已验 | 0..2 / 0..1 / 1..128；默认1/1/1，实际采样；显式温度0且无额外约束保留greedy MTP |
| penalties / seed | A03子组已验 | presence/frequency -2..2，按本choice生成历史；精确signed int64 seed，逐choice独立RNG |
| stop | 已验，有限profile | 字符串或最多4串，每串256 UTF-8字节；null/[]禁用；匹配包含推理/工具标记，截断工具错误；严格输出/cache组合拒绝 |
| logprobs / logit_bias | A03子组已验 | 原始模型分布，top_logprobs 0..20；词表内ID的-100..100 bias实际执行；边界token保留真实bytes |
| prediction | A03已验 | 字符串/text parts目标验证、部分回滚与accepted/rejected usage；拒绝token计入预算/配额。真实JSON/SSE/strict/bias/取消后MTP通过，见[prediction交付](SERVE_PREDICTION_2026-11-08.md) |
| response_format | 已验，有限 schema | text / null / json_object / json_schema；与非空 tools 同用须 none |
| stream_options.include_usage | 已验 | 各choice块usage:null，全部finish后一次汇总usage；请求logprobs时携带真实概率；中断不保证收到尾帧 |
| chat_template_kwargs | 已验，Q4T 扩展 | thinking/preserve/effort/vision ID；能力端点给出范围 |
| unknown fields | 明确拒绝 | 不静默丢弃；Responses/store/metadata 等不可直接透传 |
| 错误 JSON | 已验，Q4T 错误码 | 显式输入位置param、统一封套与追踪头；01c补齐工具/控制/递归schema路径，转义键不混淆层级；无法归属输入字段的运行错误仍可null |
| SSE 错误 | 已验 | 首帧后HTTP200内error＋DONE；部分写失败直接截断，不追加第二终态 |
| POST /v1/requests/cancel / deadline | 已验，Q4T 扩展 | 202仅接受，404不证明未执行；03c准备tokenizer等待已检查自身取消，其他锁/GPU在途计算无硬抢占保证 |
| request_id / session_id | 已验，Q4T 扩展 | 活跃 ID 去重 / 同进程 KV 缓存；不是持久任务与幂等键 |
| X-Request-Id | 已验 | 128字节受限ASCII，自动128随机位；与JSON请求/取消ID独立，不提供幂等 |
| GET /healthz、/livez、/readyz | 限定已验 | 三者语义不同；TCP连接成功不等于ready；GPU/PLE永久故障、排空、内存压力或必要内存观察失效关闭ready；辅助NVML/日志/metrics失能不据此判健康模型失效 |
| HTTP媒体 / 方法 / query | 已验 | application/json UTF-8；405＋Allow、query400、GET body400；连接关闭 |
| 控制入口 / 慢读 | 限定已验 | 4控制连接预留；业务/观测满仍健康和有效取消；普通增量流真实EAGAIN后取消回收；不保证慢头洪泛 |
| GET /metrics / 观测入口 | 12b/12g/12l限定已验 | 公共4观测/4控制；可另启loopback运维监听，136公共慢头满时已验可用；每个入口自身仍有上限，监督器观测有总截止并响应stop |
| POST /v1/chat/preflight | SRV-07限定已验 | 共用合同/模板/分词/预算；可选prompt/IDs；不预留资源或保证执行 |
| GET /v1/config | SRV-07/02b/12b/02i限定已验 | 实际容量/来源、准入/等候、连接分区及媒体解码/函数能力；私有v1 schema/指纹随合同更新，固定消费者需选版本；不暴露模型目录/凭据 |
| 请求准入 / slot队列 / RPM/TPM | [客户端治理](SERVE_CLIENT_GOVERNANCE_DELIVERY_2026-11-05.md)限定已验 | chat/preflight共享上限；v2策略显式启用分钟请求/token额度及准备后按client轮转准入；默认策略关闭时维持容量FIFO，不保证GPU时间/时延公平或RAM预留；新service恢复等待完整分钟窗口，原生进程额度本身不持久化 |
| 内存准入 / 申请额度 | 02c至02e/02j2/02k限定已验 | MemAvailable准入、生产CUDA申请额度、可选host记账域；各有计量口径，不能合称Thor整机物理RAM硬上限 |
| 设备/host资源错误 | 02f至02i限定已验 | 已知OOM恢复，未知设备/释放错误停服，host固定错误封套，视觉加载失败拒绝启动；不覆盖所有外部库故障或物理OOM |
| 机器schema | SRV-08限定交付 | 38字段/10路由OpenAPI加必需原生语义清单；396 CPU差分，实际25 wire/56封套；标准schema本身不证明动态准入 |
| Responses / Assistants / Batch | 未实现 | 不能通过更换 base_url 使用 |
| 图片 / 视频 / 音频 | 非首批文本兼容范围；图片/两帧实验限定通过 | 02h+02i红图/两帧、准入/配额与取消恢复已验；需显式无MTP媒体配置，不扩广泛质量；音频未支持 |
| OpenAI Python SDK | 3.42.1 限定互通通过 | models、JSON/SSE、工具往返、预算与参数错误；关闭自动重试 |
| OpenAI JS SDK | 7.47.0限定HTTP通过 | Node24.18.0；models、JSON/SSE、真实工具成功/错误/双结果、截断后显式答案恢复；完整终态批次门，零自动重试 |
| 其他 SDK / 框架 | 未验证 | 不从 Python / JS 或自有客户端通过外推 |
| 鉴权 / TLS / 多客户端隔离 | 原生入口及runner策略已短验，外部TLS按需启用 | 显式Key、CIDR与预算；实际LAN、正式证书及通知启用时验收；不提供完整多租户计费或浏览器CORS接入合同 |


实现入口：[请求合同](../src/server/chat_contract.cpp)、
[模板](../src/server/chat_template.cpp)、[能力声明](../src/server/chat_capabilities.cpp)、
[HTTP接入](../src/server/chat_http.cpp)、[输出](../src/server/http_response.cpp)。
工具的执行与业务参数校验属于应用；runner发布完整调用，不能把生成的
工具名、参数或资料文本当作运行任意代码的许可。

## 客户端与恢复边界

| 入口 | 可用合同 | 不能外推的部分 |
|---|---|---|
| 标准库`ChatClient`与项目示例 | JSON/SSE完整终态、工具整批验证、显式取消、Key/CA/IP配置、全程网络截止与截断拒绝 | [08b短验](SERVE_CLIENT_TRANSPORT_RESULT_2026-11-05.md)；用户回调/CPU工作不可抢占，不声称硬实时 |
| 项目JS SDK适配器 | 固定Node24.18.0/openai7.47.0、总截止、原wire/SDK交叉核对、独立Key/CA/IP、零重试 | [08b真实TLS/工具短验](SERVE_CLIENT_TRANSPORT_RESULT_2026-11-05.md)；内部消费流后返回完整事务，无UI delta回调；不外推其他SDK版本 |
| 持久任务客户端 | [11j全程TCP/TLS/写/读截止](SERVE_TASK_DEADLINE_RESULT_2026-11-04.md)、client绑定、预算/预检、显式恢复与迁移 | [当前交接](SERVE_CURRENT_TASK_RESULT_2026-11-05.md)只支持登记的有向单步，含15d7ccb1→cc321239与精确17/18来源；不自动跳版本/换模型/换地址，归档迁移接受路径和连续三跳仅CPU证明 |
| 官方OpenAI SDK或其他应用 | 走已支持的Chat/API子集及扩展参数；调用方管理连接、重试和工具 | 本项目只验表内固定Python/JS版本；无全SDK、全框架或全部OpenAI平台等价保证 |

持久任务的幂等键、事件日志、工具结果和永久保键属于应用层；
`request_id`只去重活跃请求，`session_id`只标识KV状态，均不能替代
任务库。备份目前为源可核对的同机单活交接，不支持源盘丢失后强制
启用旧副本，也不宣称通用exactly-once。

## 当前剩余项与顺序

按用户纠正，当前剩余能力进入[Serve总计划](SERVE_MASTER_PLAN_2026-11-08.md)，
不能再将采样、多choice、Responses、原生TLS/CORS等写作“按新场景
另立项”后结束任务。外部组件可选，项目所需能力须原生实现。

1. R01/R02先完成日志观察阻塞隔离、grammar有界回收。
2. N01—N03与A01/A02补原生TLS/LAN/证书/CORS及标准输入/模板合同。
3. A03—A07补真实采样/多choice/logprobs、工具增量流、官方schema
   支持范围、Completions与Responses；声明拒绝只是当前状态。
4. A08/A09及N04—N07补标准持久对象/Files/Batch、客户端治理、
   HTTP与原生运维；R03/R04补真实进度与观察留存。
5. R05—R10补自动profile/版本回退、冷启动和资源保护/视觉恢复；
   A10—A12补模型能力接入、工具适配及标准SDK，解除旧包固定限制。
6. Q01—Q03完成真实资料查询、可回退发行和全部任务核对；长期
   稳定性驱动交用户实跑。没有现场域名不妨碍原生能力的开发/本机验证。

上表继续只反映实际已支持能力，不提前改为“已实现”。既有SDK、
配额、工具和恢复证据按身份复用；新组合需对应证据。不能以功能表
覆盖率给出工业化成熟度百分比，也不能仅列限制就认定解决了缺口。

## 冻结阶段

旧P0至P3计划与预算见[历史快照](APPLICATION_SERVICE_HISTORY_2026-11-04.md#冻结阶段)。
后续每批重新冻结受影响合同、有限预算和退出条件；已通过且身份未变
的证据直接复用，不以整理文档为由再跑模型。

## 本轮验证与交付合同

2026-11-04静态复核见[原记录](SERVE_CONTRACT_REAUDIT_2026-11-04.md)。
2026-11-05客户端、托管与任务交接分别完成有限CPU/HTTP/模型验收，
完整边界和剩余事项见[最新交接](SERVE_CLOSEOUT_HANDOFF_2026-11-05.md)。
本页更新不追加推理，仍未开展当前发布长稳或正式LAN验收。

## 官方协议依据

最初官方协议核对日期为2026-11-02，来源与当时结论保留在
[历史依据](APPLICATION_SERVICE_HISTORY_2026-11-04.md#官方协议依据)。2026-11-08已重新
核对官方Chat、Responses迁移、工具/结构化/Batch与vLLM接入文档，
来源和获取局限见[重审证据](evidence/serve-goal-reaudit-20261108/README.md)。
重新审计不表示新兼容能力已经验收。

## 首批交付历史

[原始逐阶段快照](APPLICATION_SERVICE_HISTORY_2026-11-04.md)保留曾经的待办、
默认身份、预算及失败。当前能力以本页和源码为准，各阶段实际接受
范围仍以对应结果文档及原件为准；历史中的“未验”不自动表示当前缺失。
