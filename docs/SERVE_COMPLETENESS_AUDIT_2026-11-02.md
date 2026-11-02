# serve 应用服务层完备性审计

后续进展：审计 A 组 F1/F2/F3/F4/F7 已于 2026-11-02 按冻结范围
修复、真实业务验收并交付，默认 ea678009 / 90372798，详见
[输入合同结果](SERVE_INPUT_CONTRACT_2026-11-02.md)。下文保留初次
7606435 审计的缺陷证据。后续 F5 与 F6 独立准入首组也已验收并
交付 f03ee0d4 / a1e9d24e，见[观测与资源结果](SERVE_RESOURCE_BOUNDARIES_2026-11-02.md)；
F6随后以exec worker、2GiB RLIMIT_AS、10秒编译/2秒匹配、取消kill/reap
交付c8c412c4/da9f3ac5，见[冻结边界与失败/复用证据](SERVE_GRAMMAR_ISOLATION_2026-11-02.md)。
这给出工作进程地址空间硬限额，不是整机RAM或独立RSS保证；B组其他
请求头ID/心跳/背压等在该F6交付时仍未整体完成。以下F6正文保留初次审计事实。

2026-11-03 [serve阶段A限定交付](SERVE_STAGE_A_RESULT_2026-11-03.md)：
G4的追踪身份/部分错误定位、G5的nullable/心跳/普通流实际背压、G6
的媒体/方法/query、ready/live/drain与控制入口预留已有证据。G4预检、
全部嵌套param、严格长payload背压、所有代理/长prefill、完整SDK与
G10组合矩阵仍未整体闭合。下文是初次审计，不能当作当前遗漏列表。

2026-11-03 [阶段B1角色profile交付](SERVE_STAGE_B1_RESULT_2026-11-03.md)：
G1已补单条开头developer适配、共用角色/指令内容校验和明确name拒绝，
46模板正例逐字节/ID、12拒绝形状及真实工具往返通过；不代表混合
指令层级、非null name或任意模板支持。G2推理控制、G3 stop与预检仍待办。

日期：2026-11-02。审计基线 `7606435`；运行时源码锚点 `6bf61b7`，
默认二进制 `957211c7` / sidecar `9eba5472`。本轮没有修改运行时、
重新部署或新增模型请求。结论是：**当前为经过限定验收的私有文本
服务子集，尚不具备完整的 OpenAI 接入合同和通用应用服务完备性。**

工具 / 推理解析、严格工具控制和有限结构化输出已经存在；这次重点
是寻找输入到业务闭环中仍会丢失语义的路径，不以新增 API 数量评分。
发现 4 类实现问题（F1/F2/F4/F5）、1 类标准兼容缺口（F3）、1 类
已披露但尚未封闭的资源风险（F6）、1 类低优先级数值输入问题（F7）。
均未在本轮修复；未发现有证据支持的 P0/P1 故障。

## 范围与证据口径

- 覆盖实际 6 条 HTTP 路由、35 个 Chat 顶层字段、10 处直接白名单；
  追踪输入校验 → 模板 → 预算 / 准入 → 生成 → 输出解析 → JSON/SSE
  → 客户端工具执行 → 历史回填，以及错误 / 取消 / 关闭 / cache 提交。
- 固定 51 个服务相关源码锚点，包括 reader、scheduler、配置、JSON、
  tokenizer 和构建身份。锚点用于检测审计过期，不表示逐行形式验证。
  模型数值、MoE/SSD kernel、第三方语法编译器内部不作新一轮深审。
- 本地 vLLM 固定 `67111973eeb104e83bc6707a2d0f139737b711a0`，
  只读 `reference/vllm`。官方网页于本日核对；stable 文档不等同此
  固定源码版本。vLLM 也有依赖 parser / 模型 / 配置的能力边界。
- 新证据为 **20 个生产合同 / 模板 / 输出 parser 探针、10 个生产
  HTTP reader / dispatch socketpair 探针**。后者 Chat 生成 handler
  是显式 stub；取消 handler / registry 使用生产代码。没有监听端口、
  CUDA 调用或模型生成，不把 stub 到达计成业务成功。
- 原有模板、数值、HTTP / SDK / 业务交付结果按身份复用。此次没有
  重跑全部测试，也没有新增并发、性能、跨 SDK 或长期稳定性结论。

证据入口：[原始观察](evidence/serve-audit-20261102/run-repair/observations.jsonl)、
[夹具](evidence/serve-audit-20261102/run-repair/fixtures.jsonl)、
[结果与源码身份](evidence/serve-audit-20261102/run-repair/result.json)、
[互斥阶段结果](evidence/serve-audit-20261102/lease-repair/exit.json)。
`all_observations_match=true` 表示复现与当前行为一致，**包括错误行为**，
不表示这些问题已修复或服务通过新的发布验收。

## 优先发现与修复出口

### F1 · P2 · 工具定义在普通 auto 路径没有完整嵌套白名单

位置：`chat_contract.cpp:167`、`output_constraints.cpp:108/156`、
`chat_response_parser.cpp:180`。顶层字段严格不等于内部字段严格。
默认 auto、没有合法 `function.strict=true` 时，约束规划提前返回；
parser 校验已知结构，却不拒绝工具 / function 内的未知字段。

可复现的错误输入是把 `strict:true` 放在 `tools[0]`，而不是
`tools[0].function`。当前请求被接受、`constrained=false`；给定模型
输出 `lookup({})`，即使声明 `query` 必填，parser 仍产生完整调用。
`parameters` 拼成 `paramaters` 也被接受。有效严格请求不受此反例影响：
对照将 strict 放回正确位置，相同输出被 `missing required property`
拒绝。**不是合法 strict 被绕过，而是错误控制字段被静默接收。**

修复出口：普通 auto / raw / none / named 共用工具定义形状校验，
拒绝未知控制字段和错类型；把 schema 的“提示语义”和“严格保证”
明确区分。测试正确 / 放错层级 / 拼错 / null，确认请求在推理前拒绝；
保留已验普通 auto 和合法 strict 的 JSON/SSE 工具往返。

### F2 · P2 · 历史工具调用及结果仍能静默丢失内容

位置：`chat_tool_history.cpp:19/80`、`chat_template.cpp:181`。
带 ID 路径验证 name/type/id 和结果关联，但不要求 arguments 存在，
也不拒绝 function 未知字段。`arguments` 拼成 `argumens` 后，模板
继续渲染空参数调用；探针中的 `AUDIT_SENTINEL` 参数完全消失。
带 ID 的 tool 结果缺少 content 仍被当作空结果，完成整批关联。
无 ID 历史兼容分支甚至把 `type:custom`＋function 渲染成函数调用。

修复出口：带 ID 的标准历史需要完整 function/arguments、合法 JSON
对象和显式结果内容；空字符串结果与“没有结果字段”分开。未知嵌套
字段拒绝。保留 legacy 只能是明确登记的兼容形状，不因没有 ID 就
跳过 type/name/参数形状。覆盖乱序、缺失、重复、未知 ID、业务失败
回填及正常历史往返；不能破坏已冻结模板夹具而不说明兼容选择。

### F3 · P2 · 已接入字段的 null 语义不完整

位置：`chat_contract.cpp:88/119/121/146`。
八个独立探针确认 `temperature/top_p/n/frequency_penalty/
presence_penalty/stream/stream_options/user: null` 均被拒绝。
当前公开 OpenAI Chat 合同允许这些字段使用 null；通用客户端序列化
可选值时会遇到 400。固定 vLLM 请求类型也将这些值声明为可空。
现有 Python SDK 3.42.1 的限定互通成功不能覆盖这种请求形状。
[OpenAI 字段定义](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

修复出口：逐字段定义 missing/null/empty/neutral 的归一化与错误规则。
null 可回到**本项目已声明的 greedy 默认值**，不因此开启未实现的
采样。保留真实非中性采样参数的拒绝；验证 SDK 序列化后的实际 body，
并在机器能力接口中明确默认值。不得机械允许所有字段 null。

### F4 · P2 · 取消端点不执行同等级请求合同

位置：`chat_http.cpp:239`。取消只校验 JSON 对象和 cancel_token，
未知字段不拒绝、request_id 缺失退成空字符串。
真实 dispatch 探针发送正确凭据外加 `dry_run:true`，得到 202，
registry 实际转入 cancelled；缺失 request_id 则返回 404 而非参数错误。
`dry_run` 从来不是支持的功能，错误是未知控制字段被忽略。此反例
只取消了探针自建的内存请求，未操作任何运行中的业务。

修复出口：独立 cancel schema，只允许必需的 request_id/cancel_token；
缺失、类型错、未知字段先拒绝；正确凭据 202、错误凭据 404 保持。
202 仍仅意味着接受取消，不承诺 GPU 已释放或此前业务副作用撤销。

### F5 · P2 · 指标名称与计时 / 结果边界不一致

静态数据流确认，未为此重跑模型：

- `chat_generation.cpp:376` 的 `queue_seconds` 从 `t_arrive` 起算，
  包括验证、模板、grammar compile；显式 slots 时还包括 tokenization。
  `/metrics` 却将其标作 `Seq-slot queue wait`。排障会把 CPU 编译 / 分词
  误判为排队。现有 `RequestDiagnostics::Wait::kSlot` 有更准确边界。
- `chat_generation.cpp:1733` 在响应解析、完整性验证、最终写出之前
  记 `e2e_seconds`；HELP 写成端到端请求时延。`response_seconds` 才
  对应完整本地写出，二者仍都不等于客户端收到响应。
- `chat_metrics.cpp:70` 将 aborted 描述为客户端断连，实际还计入
  deadline、显式 cancel、shutdown；请求原因没有对应低基数分类指标。

修复出口：定义计时起止和所有终态，改观测点或名称，保持兼容迁移。
使用可控延迟的合同验证“编译慢但 queue=0”和成功 / 四种取消原因，
确认 dashboard / 性能分析选用正确指标，不以单次吞吐估计掩盖口径。

### F6 · P2 · 结构化编译尚无独立资源 / 取消上限（已知限制）

位置：`structured_decoder.cpp:51`、`chat_generation.cpp:277`、
`server_options.h:16`、`chat_http.cpp:181`。schema/工具数/输出字节
已有界，grammar 完成后也检查 64 MiB，但**不是编译峰值 RAM 上限**。
每个请求重新构造 vocabulary、tokenizer info 并同步编译；最少 32 个
chat 可处于准入阶段。单编译线程不等于全服务一次只编译一个 grammar。
取消在编译前后检查，无法中断编译内部；关闭等待所有 handler 退出。

这是上一交付明确披露的边界，不是新发现的实际 OOM 或服务挂死；
本次没有故意压垮机器。修复出口：编译独立限流、成本预算 / 有界缓存
身份、可观察编译等待，以及超时 / 关闭策略。扩大并发前冻结少量
“普通＋严格＋取消＋慢客户端”混合场景，同时计量统一 RAM。不能
通过强行释放仍执行中的对象模拟取消，也不能把进程外杀作为通常语义。

### F7 · P3 · 整数参数先经 binary64 舍入，存在接收非整数的边缘

位置：`chat_contract.cpp:97`、`src/io/json.cpp:350`。
原始 JSON `max_tokens:1.0000000000000001` 被舍入成 1，整数检查通过；
`1.5` 正常拒绝。输出 schema 已有精确文本整数判定，输入预算还没有
统一使用。该反例不突破预算上限，但违反“必须为整数”的输入合同。

修复出口：在 JSON 数字原文上验证整数性，再做范围转换；检查预算、
deadline、prefix checkpoint、seed、数组界限等同类字段。边界拒绝
测试即可定位，随后复用未变数值引擎证据。

## 全链路覆盖与仍缺能力

下表的“已有”都限定在原交付范围。F 为上面的具体发现；G 为功能 /
运维缺口，不能混成“当前代码都坏了”。优先级是本项目应用目标的顺序，
并非要求一次实现 vLLM 的所有功能。

| 环节 | 已有实现 / 证据 | 未闭合项与下一门槛 |
|---|---|---|
| 启动与发布身份 | 参数先验证，模型加载成功后监听；sidecar、回退与健康验收 | 有效 max_len/slots/default budget/cache 模式没有完整发现接口（G4） |
| HTTP 帧与接入 | HTTP/1.0/1.1、Host/重复 framing 拒绝；绝对读取时限；body lease | Content-Type 未验证，错误方法 / query 返回 404；G6 |
| 路由 | healthz/models/capabilities/metrics/chat/cancel 六条 | 不提供 Completions / Responses 等，不能把 raw prompt 当独立 Completions API |
| JSON 资源与形状 | 严格 UTF-8、重复 key、深度64 / 65536值 / 字符串16MiB | F1/F2/F3/F4/F7；未知字段在不同嵌套层处理不统一 |
| 消息角色与内容 | system/user/assistant/tool；文本串/part；工具 ID 归一 | developer/name/refusal/annotations 的实际语义未实现，非空值拒绝（G1） |
| 模板边界 | 原生 qwen4_exp，模板 / 夹具身份；options 白名单 | system 仅首条、需要 user；非任意模板 / assistant continuation；legacy F2 |
| 工具选择 | auto/none/required/named、parallel=false 已验 | F1；普通 auto 不保证 schema；true 不等于应用并发执行 |
| 结构化输出 | 固定 XGrammar、有限 schema、最终独立校验、截断报错 | F6；非完整 JSON Schema / 任意 grammar；声明 unsupported keyword 拒绝 |
| 推理解析 | reasoning_content 与 content 分离；长度中止 / 块闭合检查 | reasoning 新字段适配、隐藏输出、独立 thinking budget 未实现（G2） |
| 停止与上下文 | generation_config EOS 集合、两预算别名、容量检查 | stop 字符串 / token IDs、预检 / token count、详细预算反馈未实现（G3） |
| 采样 / 概率 | 固定 greedy、n=1，中性值兼容 | sampling / multi-choice / logprobs / bias 需要引擎支持（G8），不能只放开字段 |
| 普通文本与 UTF-8 | JSON/SSE 共用 parser、跨 token UTF-8、raw 开关 | 原生标记与字面文本解释边界应有 fixture；不能从 prompt 注入推出服务执行授权 |
| SSE 输出 | created 稳定、tool index/ID、完整调用发布；终态顺序已验 | 工具参数非逐字增量，结构化整份缓冲；无 heartbeat；G5 |
| 用量 | prompt/completion/total、可选 SSE 用量尾帧；cache细分 | 无 reasoning token 细分；include_usage 的其他帧缺 usage:null（G5） |
| 错误 | HTTP/SSE error 封套、首帧前后错误、截断不发成功终态 | param 多为 null；缺统一 request header / 原因分类和错误 schema 注册（G4/F5） |
| 取消与 deadline | 断连、显式、deadline、shutdown 协作取消；令牌保护 | F4/F6；header/body 读取是另一时限；202 不等于 drained |
| 负载与队列 | 连接 / body / active chat / GPU slots 分层上限 | 无明确公平 / 优先级 / 租户配额；health/cancel 与业务共用连接入口（G6/G9） |
| 生命周期 | RAII、GPU sticky unhealthy、scheduler/slot 回收、异常终态已有证据 | F6；未重新证明所有新旧功能混合取消 / 背压 / shutdown（G10） |
| cache | 显式 SSD 会话 / prefix、同进程身份、失败不发布快照 | session_id 不是完整对话、磁盘 cache 不是重启恢复；严格请求拒绝 cache 组合 |
| 健康 / metrics | GPU 健康、槽计数、请求诊断、Prometheus | F5；liveness/readiness/draining、负载下控制入口独立保障未闭合（G6） |
| 请求身份与配置 | JSON request_id，协议/模板 source hash，发布 sidecar | 自动计数器跨重启重复；x-request-id、模型别名/配置发现未实现（G4） |
| 工具业务执行 | 正式客户端验证批次后执行并回填；真实只读资料闭环 | 外部 SDK/框架未普遍验收；schema 正确不等于参数授权或结果正确（G9） |
| 持久记录 / 恢复 | 应用保存响应/工具结果，未知执行结果禁止自动重放 | 不是 serve 持久任务库、exactly-once 或通用自动重试（G9） |
| 网络入口 | 默认 loopback，可显式 LAN；已有 Nginx 示例 | 原生无 auth/TLS/CORS；真实网关访问策略、配额和断线须部署时验（G9） |
| 客户端合同 | 自有客户端、Python SDK 3.42.1 限定互通 | F3/G5；无 OpenAPI、JS/其他 agent SDK 完整合同（G10） |

G1/G2/G3/G4/G8/G9 的既有缺口继承
[32项对照](VLLM_SERVICE_GAPS_2026-11-02.md)；此处补充容易被单功能
测试掩盖的 G5/G6/G10：

- **G5：流式可用性与 wire 完整性。** header/role 首帧在 prefill 后
  才发送；结构化解码期间 payload 缓冲且没有心跳。不能把 server 的
  20分钟默认 deadline 当成代理 idle timeout 保证。普通增量帧缺少
  `usage:null`，choice 也省略 `logprobs:null`；宽松 SDK 可接受不代表
  严格 schema 客户端相同。须分别定义首字、心跳、结果发布与用量合同。
  [OpenAI SSE 说明](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- **G6：HTTP / 控制面。** `text/plain` 的有效 JSON 到达 Chat stub，
  POST models / 带 query 的 models 均 404。Expect 与 Transfer-Encoding
  实际已经拒绝，不应误报为等待或支持；每连接关闭，无 keep-alive、
  HTTP/2 / chunked 上传合同。连接满时取消和健康检查也走相同拒绝门；
  这是可用性设计边界，本次没有进行连接洪泛或宣称发生过故障。
- **G10：组合验收。** 结构化交付只有 full head / 单槽 / 8192 的有界
  结果；不能与其他阶段的多槽 / cache 成绩拼成“全组合已验”。优先
  明确拒绝不支持组合，并对允许的新组合逐阶段选择代表用例。

Responses、Assistants、embeddings、rerank、音频、广泛多模态、LoRA、
分布式管理和动态权重不是当前私有文本目标的遗漏修复项。vLLM 的
相关端点也依赖模型与服务配置，不能仅凭有端点推导本模型能力。

## 防止再次漏项的实际机制

新增[机器清单](../tools/serve_audit/inventory.json)与
[检查器](../tools/serve_audit/check_inventory.py)，已接入公共 host CI：

1. 每个现有路由 / 顶层字段必须有语义、限制与证据引用；新增或删除
   后不更新清单则失败。直接嵌套白名单变化也使清单过期。
2. 服务源码、取消 / reader / scheduler / 配置 / tokenizer 任一锚点
   变化，必须重新审阅受影响合同；不能只刷新 hash 后继续沿用旧结论。
3. 本机检查清单与三类故意变异：未登记字段、未登记路由、源码变化。
   三者均必须被拒绝。清单通过只表示盘点未过期，不签发功能验收。
4. 后续一个功能完成的最小闭环是：输入类型 / null / 未知字段 →
   模板与预算 → 非流式和流式输出 → 错误 / 取消 → 外部客户端消费 →
   工具结果回填或业务结果 → 能力声明、发布身份与证据同步。
5. 每阶段冻结代表组合，不做盲目全笛卡尔积：thinking 开关 × JSON/SSE；
   auto/none/required/named × 单/多调用；合法/错误/截断 × 首帧前后；
   普通/严格 × 取消/背压/关闭；历史 ID / 错误结果 × 恢复记录。

清单无法自动理解语义，源码 hash 也无法证明正确。外部 OpenAI/vLLM
协议更新、编译选项与实际配置差异仍要人工核对；不能保证以后零缺陷。
它的用途是使“已接收字段但没检查后半条业务链”更难被当作完成。

## 后续分组与交付边界

| 阶段 | 冻结工作 | 可交付的退出条件 |
|---|---|---|
| A 输入合同收口 | F1/F2/F3/F4，顺带精确数值 F7；不新增广泛功能 | 正确/错误形状 CPU 合同＋少量真实 JSON/SSE 工具往返；无静默降级，SDK null 请求一致 |
| B 观测与资源 | F5、F6、G4/G5/G6 的必要入口部分 | 计时口径合同、编译准入/取消/关闭、统一 request ID；一次有界混合请求与内存观察 |
| C 应用接入 | G1/G2/G3，再补 G9/G10 的目标业务 | developer/name/reasoning 策略、停止/预检；真实项目查询、工具业务失败、跨进程恢复边界和目标 SDK 验收 |

先关 A，不能因已有结构化成功样例而跳过。B 的编译资源门应先于
更广并发服务；C 按真实应用需要展开。每组仍按既定有界测试、失败
留证、默认替换/回退与阶段提交规范进行；本轮只交付审计和防漏门。

## 本轮执行与保留

首次 probe 构建缺少 IO 静态库，链接失败，guard `COMMAND_FAILED`；
[原始错误](evidence/serve-audit-20261102/run/build.stderr)与构建命令保留。
补足与现有无模型 lifecycle 探针相同的依赖后，零警告构建和固定 30
例在 **15.218 秒**内完成，guard `COMPLETED`，没有 ENV_ABORT。未为
已通过观察重复扩样，两个阶段均未加载模型。

归档见[封条](evidence/serve-audit-20261102/manifest.json)。探针可执行
只在 `.q4t-work/serve-audit-20261102/run-repair/probe`；复现步骤见
[工具说明](../tools/serve_audit/README.md)。默认 binary/sidecar 的完整
SHA256 均记录在 result，与前一交付一致。模型 / reference 保持只读。

参考：[vLLM 服务接口与请求头](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/)、
[工具 strict 的层次与配置限制](https://docs.vllm.ai/en/stable/features/tool_calling/)。
主要借鉴其协议模型、parser、输出保证和 serving 生命周期的分层，不
把支持项数量或网页目录当成本项目成熟度证明。
