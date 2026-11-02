# 应用服务层与 OpenAI 兼容矩阵

更新：2026-11-02。范围由用户确认：OpenAI 兼容 API 优先，真实业务为
只读查询本项目文档、发布与验收记录。代码优先；实现、验证和业务质量
分别记录。此页是持续维护入口，历史验收不随新功能自动扩张。

2026-11-02 后续[工具控制与结构化输出已交付](STRUCTURED_OUTPUT_RESULT_2026-11-02.md)：
默认 957211c7 / 9eba5472，固定 grammar 后端与有限 schema profile
通过有界 SDK / HTTP / 项目读取验收。实现合同见
[profile 与组合限制](STRUCTURED_OUTPUT_DEVELOPMENT_2026-11-02.md)。

## 边界与成熟度

当前具备可用的私有文本推理服务和有界工具调用链路。尚不能宣称通用
OpenAI API 全兼容或生产多租户应用平台。模型支持、模板一致、输出可
解析、客户端可接入和业务答案正确，是五个不同的验收层级。

| 层次 | 已有实现 / 证据 | 尚欠能力 |
|---|---|---|
| 推理与请求生命周期 | GPU 错误隔离、容量准入、取消、超时、槽回收；已有 Thor 验收 | 与应用协议分开验收；不把本轮当性能优化 |
| 消息与模板 | 原生 qwen4_exp 模板、工具历史 ID 对齐、推理内容分离；21 模板用例 | 并非运行任意 HF Jinja 模板；新增模型需要独立适配 |
| 输出协议 | JSON / SSE、工具控制、有限 strict/schema、usage、统一错误、稳定 created、两种 token 预算字段已验 | 完整 schema、停止序列和逐字工具参数增量未实现 |
| 客户端 | 项目客户端、持久记录、只读恢复判定；OpenAI Python 3.42.1 限定互通通过 | 其他 SDK / 框架未验；没有自动重试 / 任务恢复执行器 |
| 应用工具 | 真实发布、历史验收与不存在资料查询通过；来源范围、工具业务错误与预算门已验 | 自主广泛搜索、逐句事实验证与实时状态查询未验 |
| 服务运维 | health、metrics、代理模板、可核验发布与回退 | 内置鉴权 / TLS / 租户配额 / 持久任务服务不在当前范围 |

Transformers 的模型与模板接口、vLLM 的推理服务接口、Q4T 的 Thor
原生 runner 是不同层次。此矩阵评价 Q4T 对外可用合同；不从底层模型
支持推导应用兼容，也不把外部项目的“支持 OpenAI”解释为每字段等价。
此前模型调查入口见 [参考项目](REFERENCE.md)。

## API 兼容矩阵

当前交付为 957211c7 / sidecar 9eba5472，盘点起点为 460ffd0 /
旧默认 830694ce。`已验`指本项目已有明确证据，不代表外部 SDK 全部
版本通过。P1 / P2 的验证范围与具体失败在本页末尾及结果文档记录。
完整 vLLM 对照与优先级见[32项服务能力盘点](VLLM_SERVICE_GAPS_2026-11-02.md)。

| API / 能力 | 当前状态 | 限制与证据 / 行动 |
|---|---|---|
| POST /v1/chat/completions | 已验，子集 | 单模型、单 choice；chat_generation.cpp |
| GET /v1/models | 已验，SDK 3.42.1 | 单个配置模型；不支持模型详情 / 管理 |
| GET /v1/capabilities | 已验，Q4T 扩展 | 实现身份，不是发布或质量证明 |
| messages | 已验 | 默认解析；system/user/assistant/tool；模板决定角色边界 |
| prompt / parse_response=false | 已验，Q4T 扩展 | raw 文本；不解析工具或推理 |
| JSON / stream=true | 已验 | SSE finish → 可选 usage → DONE；中断不能当完整响应 |
| content / reasoning_content | 已验 | reasoning_content 是扩展字段；不等于 OpenAI reasoning item |
| function tools / tool_call_id | 已验 | ID 唯一完整批次；文本结果可乱序归一；客户端执行 |
| tool_choice | 已验，子集组合 | auto / none / required / named；named 恰好一个调用，显式约束模式仅 parsed messages |
| parallel_tool_calls | 已验 | false 最多一个；true 允许多调用，不代表应用并发执行 |
| strict / JSON Schema 输出约束 | 有限 profile 已验 | q4t-json-schema-v1，逐 token 掩码＋最终验证；未知关键字拒绝，应用仍须验证业务 |
| max_tokens | 已验 | 正整数；包含推理 / 模板生成 token；受上下文容量约束 |
| max_completion_tokens | 已验 | 与 max_tokens 共用生成预算；两个字段同时非空时拒绝歧义 |
| temperature / top_p / n | 部分 | 仅 0 / 1 / 1；不可声称支持采样 |
| penalties / seed | 部分 | 仅中性 penalty；seed 在 greedy 下不改变结果 |
| stop / logprobs / logit_bias | 明确拒绝有效设置 | 仅接受现有中性空值；不是实现了这些功能 |
| response_format | 已验，有限 schema | text / null / json_object / json_schema；与非空 tools 同用须 none |
| stream_options.include_usage | 已验 | 最后独立 usage；连接中断不保证收到 |
| chat_template_kwargs | 已验，Q4T 扩展 | thinking/preserve/effort/vision ID；能力端点给出范围 |
| unknown fields | 明确拒绝 | 不静默丢弃；Responses/store/metadata 等不可直接透传 |
| 错误 JSON | 已验，Q4T 错误码 | 统一 message/type/code/param 和 HTTP 状态原因；不等同云端全部错误语义 |
| SSE 错误 | 已验 | 错误 + DONE，不发成功 finish / usage；无自动重放 |
| 请求取消 / deadline | 已验，Q4T 扩展 | 202 仅接受取消；404 不证明请求未执行 |
| request_id / session_id | 已验，Q4T 扩展 | 活跃 ID 去重 / 同进程 KV 缓存；不是持久任务与幂等键 |
| Responses / Assistants / Batch | 未实现 | 不能通过更换 base_url 使用 |
| 图片 / 视频 / 音频 | 非首批文本兼容范围 | 媒体实验开关不代表多模态业务验收 |
| OpenAI Python SDK | 3.42.1 限定互通通过 | models、JSON/SSE、工具往返、预算与参数错误；关闭自动重试 |
| 其他 SDK / 框架 | 未验证 | 不从 Python 或自有客户端通过外推 |
| 鉴权 / TLS / 多租户隔离 | 不内置 | 由明确配置的应用网关承担；当前仅本地受控验证 |

实现入口：[请求合同](../src/server/chat_contract.cpp)、
[能力声明](../src/server/chat_capabilities.cpp)、
[HTTP](../src/server/chat_http.cpp)、
[响应](../src/server/http_response.cpp)、
[客户端](../tools/examples/chat_client.py)、
[恢复判定](../tools/examples/tool_recovery.py)。
已有接受证据：[聊天业务](CHAT_BUSINESS_ACCEPTANCE_2026-11-02.md)、
[正式交付](CHAT_DELIVERY_2026-11-02.md)。

## 冻结阶段

P0：完成以上盘点、明确接入子集和场景，保留基线身份。

P1：补 max_completion_tokens 与 max_tokens 共用预算；统一 HTTP / SSE
错误封套，状态分类与稳定 Q4T code；客户端暴露错误字段；能力声明和
协议源码身份覆盖对应实现。保留不支持参数的显式拒绝，不引入假约束。
参照 vLLM 盘点新增首批项：SSE created 请求身份保持一致，未实现的
嵌套消息语义显式拒绝（允许 name/refusal/audio/annotations/function_call
为空，拒绝非空；developer 和 reasoning 别名后续适配）。

P2：新增有界项目资料查询应用，工具只读本仓库允许范围内的真实文件，
返回路径、行号和内容身份；模型输入不作为任意文件 / 命令 / URL 执行。
完整工具批次检查后执行；预期工具业务错误回填，传输失败与不完整终态
停止且保留记录。复用持久记录和只读恢复检查，不引入未知结果自动重放。
使用具体 OpenAI Python SDK 版本做本地互通验证，与自有客户端分开记录。

P3 第一组（已交付）：required/named/none、parallel=false 和有限
schema 解码；18 生成 / 12 拒绝 / 3 启动通过。受约束 SSE 完整验证后
发布，单槽 / full head / 8192 容量有界验收，不使用 MTP / packed decode，
cache / media 组合拒绝。CMake 重建须 Q4T_ENABLE_XGRAMMAR=ON。

后续独立阶段：消息 / 推理字段、停止条件、请求预检和追踪、可恢复
任务状态机、网关鉴权和租户配额；分别冻结业务合同与验收。

## 本轮验证与交付合同

本轮为业务功能 / 缺陷修复，不作性能优化收益声明。先成组开发与 CPU
合同，再进行一次有界真实 HTTP 业务验收；只因具体失败重验受影响项。
GPU 阶段完整驱动持共享锁；检测竞争 / 监控失败为 ENV_ABORT，停止自有
进程并保留现场。结束后 GPU 释放，不启动常驻服务。

原冻结预算：最多 3 次模型启动（候选、MTP 配置、安装健康）、16 次生成、
12 个负例 HTTP、30 分钟 GPU 阶段；原始请求 / 响应 / 工具结果 / 模型
进程日志均留存。验证文本质量、工具事实与引用、预算生效和错误分类；
不重跑已通过且身份未变的内核数值、模板和性能矩阵。交付前保留旧
二进制 / sidecar、候选源码身份、构建配置和可核验回退入口。

驱动：`tools/application_service/stage.py prepare|execute|finalize`。
prepare 不加载模型；execute 必须由同一个 gpu_exclusive 驱动持锁。
失败不隐式重试；安装后的完整 / 部分已知 pair 均可恢复。候选与旧
程序 / sidecar、真实 CMakeCache、运行时 manifest、源代码 bundle
及只读封条放在 `.q4t-work/application-service-20261102/package/`。
安装前持久化 `build/application-service-recovery.json`，guard 成功
后 finalize 才清除；两文件分别原子替换，不声称断电事务。

CPU 先验：138 host、22 客户端、13 恢复、9 项目工具、6 回退、3 SDK
mock 合同通过。mock 仅验证 SDK 驱动，不替代后续真实 HTTP。
默认全 head / MTP k3、缓存和 suffix 的启用策略不因服务补齐改变。

首轮实际失败与定向修正：SDK 文本 JSON/SSE、1-token 上限、8 参数
负例与 1 路由负例通过；发布查询成功。历史验收查询连续查证，在
3 轮上限前未给答案，驱动安全停止（8 生成 / 9 负例 / 1 启动），
未安装、资源归零。失败原始记录和原封包保留，不能计为业务通过。
修正应用工具结果，显式提供剩余请求 / 工具预算和最后回答提示；
历史查询上限为 4 轮，仍不保证模型一定遵循提示，超限继续拒绝执行。
只重验受影响的应用查询与剩余 MTP / 安装健康，SDK 已通过证据按同
二进制、协议、SDK 版本和相同解析方法 AST 复用。
此次具体失败修正允许累计最多 4 启动 / 20 生成；负例上限 12，原
07:41:21 UTC 开始的 30 分钟截止不重置。独立修正包与执行路径为
`.q4t-work/application-service-correction-20261102/`，入口加 `--correction`。

修正业务已通过（9 生成 / 2 启动），安装健康也通过（1 生成 / 1
启动），但末尾 SDK venv 的独立 NVML worker 缺 pynvml，流程自动
恢复旧 pair。此项记 ENV_ABORT_FINAL_MONITOR_IMPORT，原 guard 的
COMMAND_FAILED 保留；不能把查询错误当 GPU 为空。修复固定使用
系统解释器并在启动前检查，3 项失败 / 回退 / finalize 合同通过。
使用 `tools/application_service/finish_delivery.py` 封存已通过身份
和旧现场，持租约重新安装、核验 pair 与前后 NVML，再等 guard 成功
后 finalize；不新增模型启动或 HTTP，原 30 分钟截止继续有效。

## 官方协议依据

2026-11-02 核对官方文档：
[Chat Completions 请求与输出](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、
[Function calling](https://developers.openai.com/api/docs/guides/function-calling)、
[错误与状态码](https://developers.openai.com/api/docs/guides/error-codes)。
max_completion_tokens 包含可见和推理 token；工具执行属于应用端；
不同模型支持的参数有差异。Q4T 的错误 code、取消、缓存与能力端点是
本地合同，不冒充 OpenAI 平台的所有行为。

## 首批交付历史

P0 盘点与 P1 协议修补已交付；P2 真实发布查询、历史验收引用及资料
不存在三场景闭环通过，OpenAI Python SDK 3.42.1 的限定接入通过。
该阶段默认 c3772d11 / sidecar c80f1f14，结束已释放 GPU；后续
外部服务恢复观察见结果文档。194 项 CPU / 驱动
合同、累计 18 生成 / 9 负例 / 4 启动及两次失败的修正边界，详见
[交付结果与275份证据](APPLICATION_SERVICE_RESULT_2026-11-02.md)。
P3 工具控制 / 有限结构化输出后续已在 957211c7 交付（见页首）；
其余规划仍未完成。自主搜索、实时状态、逐句事实验证和更多 SDK
不能由三项有界任务通过外推。
