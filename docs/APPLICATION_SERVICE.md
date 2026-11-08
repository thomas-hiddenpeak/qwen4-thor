# 应用服务层与 OpenAI 兼容矩阵

更新：2026-11-08；客户端、托管与任务交接已完成各自独立短验。当前为单台Thor、单模型的
文本Chat服务，以及只读项目资料查询应用。工具调用、有限结构化输出、
错误/取消、运行保障和持久任务恢复已有分阶段短验；不宣称全部OpenAI
接口、所有客户端、任意配置组合或长期生产负载已验。

视觉v1新增显式单槽no-MTP图片/工具/JSON及媒体展开配额组合；
视频方向任务、硬解码未接受，旧托管/持久任务不自动迁移。
详见[视觉矩阵](VISION_SERVICE_V1.md)及[独立结果](VISION_PRODUCTION_RESULT_2026-11-07.md)。

当前发布身份和下一项工作只在[STATUS](STATUS.md)维护；本页维护能力与
边界。原逐阶段叙述移入[历史快照](APPLICATION_SERVICE_HISTORY_2026-11-04.md)，
原结果、失败、证据和日志均保留。[SRV-20合同复核](SERVE_CONTRACT_REAUDIT_2026-11-04.md)
只核对源码与证据；后续[03c锁等待取消](SERVE_PREPARE_LOCK_RESULT_2026-11-04.md)
已新增原生/真实工具短验并交付默认。

当前默认服务封装已完成[原生入口及有界自恢复](SERVE_NATIVE_INGRESS_RESULT_2026-11-08.md)：
Bearer鉴权、模型/控制器/入口恢复无需nginx或systemd；Python控制面
仍保留。151项不同CPU及3启动/18Chat真实短验通过，现安装为STOPPED，
PR #22尚未合main。旧客户端、托管与持久任务仍按各自已验身份使用，
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

## API 兼容矩阵

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
| parallel_tool_calls | 已验 | false 最多一个；true 允许多调用，不代表应用并发执行 |
| strict / JSON Schema 输出约束 | 有限 profile 已验 | q4t-json-schema-v1，逐 token 掩码＋最终验证；未知关键字拒绝，应用仍须验证业务 |
| max_tokens | 已验 | 正整数；包含推理 / 模板生成 token；受上下文容量约束 |
| max_completion_tokens | 已验 | 与 max_tokens 共用生成预算；两个字段同时非空时拒绝歧义 |
| temperature / top_p / n | A03子组已验 | 0..2 / 0..1 / 1..128；默认1/1/1，实际采样；显式温度0且无额外约束保留greedy MTP |
| penalties / seed | A03子组已验 | presence/frequency -2..2，按本choice生成历史；精确signed int64 seed，逐choice独立RNG |
| stop | 已验，有限profile | 字符串或最多4串，每串256 UTF-8字节；null/[]禁用；匹配包含推理/工具标记，截断工具错误；严格输出/cache组合拒绝 |
| logprobs / logit_bias | A03子组已验 | 原始模型分布，top_logprobs 0..20；词表内ID的-100..100 bias实际执行；边界token保留真实bytes |
| prediction | A03候选CPU已验 | 字符串/text parts目标验证、部分回滚与accepted/rejected usage已实现；实际业务和发布待验，见[prediction进展](SERVE_PREDICTION_2026-11-08.md) |
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
