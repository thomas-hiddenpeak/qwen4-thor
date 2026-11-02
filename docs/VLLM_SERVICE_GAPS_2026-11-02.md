# vLLM 对外服务能力对照与遗漏清单

用户要求（2026-11-02）：参考 vLLM 的整体服务能力，发现类似“有生成
引擎却缺工具响应”的应用层遗漏。与[主兼容矩阵](APPLICATION_SERVICE.md)
一起维护；不能仅用功能数量衡量成熟度。

## 证据口径

只读本地 vLLM `67111973eeb104e83bc6707a2d0f139737b711a0`，入口为
`reference/vllm/vllm/entrypoints/openai/` 和 `entrypoints/serve/`。
同时核对当前官方文档（访问日期 2026-11-02）；未更新、构建或启动
reference，未把 stable 网页当作本地固定 commit 的相同版本。
本项目对照基线 460ffd0，首批修复另列，未验部分不标成熟。

vLLM 的 Chat 服务组织了独立的请求 schema、工具 / 推理 parser、
structured-output 后端、stream/non-stream serving 和 middleware。
Q4T 已具备相应的原生消息 / 响应解析、请求生命周期和能力接口，
但实现的协议子集和可组合能力更小。应该借鉴接口合同与分层边界，
而不是把 Python serving 层搬进 C++ runner 或照搬所有平台功能。

## 对照清单

优先级：A=会误导已有接入，首批修补；B=直接限制 agent / 文档业务，
下阶段冻结；C=按实际部署或模型任务引入。每项都有边界，不自动成为
本轮全部实现清单。

| 项目 | vLLM 参考实现 / 官方能力 | Q4T 状态与遗漏 | 优先级 / 处置 |
|---|---|---|---|
| 工具输出解析 | chat_completion/serving.py + tool parser | 已交付 JSON/SSE function calls | 复用已有验收 |
| 工具选择控制 | auto/none/required/named；约束后端参与保证 | 仅 auto；其余拒绝 | B：分别定义保证，禁止仅提示词模拟 |
| 禁止并行工具 | parallel_tool_calls=false 对输出作限制 | false 拒绝；true 不代表客户端并发执行 | B：多调用与结果回填一起验 |
| 工具参数严格性 | strict / structural tags 受模型、parser、配置约束 | 只做解析与基本类型转换；不是全 schema 校验 | B：约束解码与应用参数验证分开 |
| JSON / schema / grammar | structured_outputs、response_format | json_object / json_schema 不支持 | B：从结构化文档查询输出需求冻结 |
| 文本停止条件 | stop 字符串 / token IDs、终态与 stop_reason | 仅 EOS / token预算 / 容量停止；有效 stop 拒绝 | B：跨 token、跨块、与工具标记组合验 |
| 输出预算 | max_completion_tokens / legacy max_tokens | 新字段原来拒绝 | A：首批共用预算，双非空拒绝 |
| 消息角色 | 接口 schema 接收 OpenAI 角色，最终受模板支持限制 | developer 不支持；system 必须在首条 | B：明确角色映射后做模板对照，不静默降级 |
| 消息 name / refusal / audio | schema 有相应字段，具体模型路径决定语义 | 基线可被忽略，导致输入含义丢失 | A：首批拒绝非空未实现字段 |
| 嵌套输入校验 | 独立协议类型与 validation handlers | 顶层严格，嵌套消息 / content 部分宽松 | A：首批补可确认的静默丢弃；其他嵌套另审 |
| 推理输出字段 | 当前官方 / 固定源码为 reasoning | Q4T 为 reasoning_content | B：版本化适配，不宣称字段相同 |
| 推理隐藏与预算 | include_reasoning / thinking_token_budget 等扩展 | 有模板 thinking/effort；无独立推理预算 | B：输出隐藏不等于停止推理计算 |
| SSE 创建时间 | serving 对一请求持同一 created | 基线逐块取时间，usage 又用另一个时间 | A：首批统一请求时间 |
| SSE 工具参数增量 | parser 维护每调用 index/ID/参数增量 | Q4T 在完整调用后发参数；不是逐字参数流 | B：先声明时延边界；正确性优先 |
| SSE 用量与中断 | usage tail、可选持续统计；断连不保证尾帧 | 已验尾帧；无持续统计 / reasoning token 明细 | B：完善字段矩阵，不假填 0 |
| 错误语义 | protocol/exception handlers 分类与封套 | 基线 5xx 标 invalid_request、部分纯文本 | A：首批统一 JSON/SSE 与分类 |
| 关联请求 ID | middleware/x_request_id.py；可选请求 / 响应头 | 自定义 JSON request_id，HTTP header 不关联 | B：自动 ID / 错误 / 日志统一追踪 |
| 幂等 / 自动重试 | 不能从 request ID 或 SDK 重试推断幂等执行 | 活跃 ID 去重，无持久幂等存储 | B：本轮禁自动重放，恢复判定已具备 |
| 模型发现 | models/api_router.py，served names / adapter 视配置 | 单模型 models；无详情、别名策略 | B：客户端发现与错误模型合同 |
| Tokenizer / 渲染预检 | tokenize/api_router.py、renderer APIs | 有离线 tokenizer / template 工具，无 HTTP 预检 | B：业务预预算，避免长请求到推理时才失败 |
| 默认生成配置 | generation_config 与 serving 配置 | greedy 固定、若干中性参数兼容 | B：明确 effective defaults 与配置来源 |
| 多 choice / logprobs / sampling | 相应生成参数与响应结构 | n=1、greedy，logprobs 拒绝 | C：需要底层支持，不是 HTTP 字段补丁 |
| 会话与 Responses | 独立 responses 路由 / stateful 路径，受配置限制 | 只支持 Chat；session cache 是同进程 KV | B：应用持久会话先于新增一套 API |
| 真实工具执行 | Chat function 调用通常由应用执行与回填 | 固定 lookup 示例已验；真实资料查询待补 | A/P2：有界只读工具、来源与业务错误 |
| 取消 / 断连 / 关闭 | serving 检查断连并传播 abort；生命周期管理 | 已有取消、deadline、断连、shutdown 与槽回收 | 保留证据；不是新的缺失 |
| 压力与配额 | 调度、准入与服务配置；不等于完整租户计费平台 | 连接 / body / chat / slot 上限已具备，无租户配额 | B/C：先错误可观测，再业务配额 |
| 健康与观测 | health/load/version、Prometheus、请求 metrics | healthz/metrics 与请求诊断已有；无统一 trace header | B：readiness、故障分类与关联 ID |
| 鉴权 / CORS / TLS | middleware Bearer/CORS；服务 TLS 配置 | 原生未提供；现有 Nginx 示例未含完整鉴权配置 | C：在真实入口配置并验收，不能认为传了 api_key 就鉴权 |
| HTTP 媒体类型 / 方法 | 框架路由与 JSON request validation | 未检查 Content-Type；不支持方法返回 404；禁 chunked upload | B：写成合同并补边界用例 |
| OpenAPI / SDK | 协议类型生成文档、官方 SDK 接入示例 | 无 OpenAPI schema；自有客户端已验，SDK待验 | B：先固定版本与实际用例，再扩客户端 |
| Embeddings / rerank / audio / 多模态 | 独立任务接口，取决于模型与 runner | 当前首版文本生成范围之外 | C：不能因为 vLLM 有端点就认为本模型有能力 |
| LoRA / 动态权重 / 分布式管理 | 单独可选管理接口 | 不在本轮单模型 Thor 交付范围 | C：不为功能对齐暴露管理面 |

源码对应：Q4T 的 `chat_contract.cpp`、`chat_input.cpp`、
`chat_template.cpp`、`chat_response_parser.cpp`、`http_response.cpp`、
`chat_generation.cpp`、`http_request.cpp`、`chat_http.cpp`、
`chat_metrics.cpp` 与 `request_control.h`。vLLM 固定源码对应
`openai/chat_completion/{protocol,serving}.py`、`openai/responses/`、
`serve/{middleware,exception_handling,tokenize,instrumentator}/`。

## 组合验收，防止下次再次遗漏

功能行通过仍不足以支持真实 agent。验收至少交叉覆盖：

- thinking 开 / 关 × JSON / SSE × tool / 普通答案；工具标记出现在
  reasoning 中不能误执行，完整与截断输出分别检查。
- 单 / 多工具 × ID 关联 × 乱序结果 × 工具业务错误；验证整批后执行，
  历史回填遵循同一模板；完整 tool_calls 也不代表业务成功。
- 成功 / 参数拒绝 / 容量拒绝 / 后端失败 × 首帧前 / 首帧后；错误不能
  变成 stop，传输中断不能自动重试已经执行的业务。
- 客户端标准参数 × Q4T 扩展 × 能力声明 × 发布身份；自有客户端通过
  不代表第三方 SDK，固定版本 SDK 通过不代表所有 SDK / agent 框架。
- 已保存响应 / 已保存工具结果 / 未知执行结果 × 进程退出；恢复记录
  与同进程 KV cache 分开，重试需要新的请求身份及明确业务策略。

这些是覆盖轴，不要求本轮做全笛卡尔积。已有同身份结果复用；首批只
测新代码影响的合同与选择的真实项目查询，后续按每阶段风险冻结用例。

## 排期建议

第一阶段完成“不能误导”的协议地基与本页盘点；第二阶段以真实项目
查询证明工具错误、引用与持久记录能闭环。之后优先：

1. 工具选择控制 + 结构化输出（需要真实约束保证）。
2. developer / name / reasoning 字段兼容、停止条件和 token 预检。
3. 请求追踪、配置声明、网关访问策略与可恢复任务状态机。

多模态、Responses、批处理、LoRA 等按用户实际场景再定，不与上述
可靠性缺口混为同一轮“全兼容”。当前成熟度仍是已验证的私有文本
服务子集，不能给出无证据的百分比成熟度或生产平台完备结论。

官方来源：
[服务 API 与扩展](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/)、
[工具模式与约束边界](https://docs.vllm.ai/en/stable/features/tool_calling/)、
[结构化输出](https://docs.vllm.ai/en/stable/features/structured_outputs/)、
[推理字段迁移与预算](https://docs.vllm.ai/en/stable/features/reasoning_outputs/)。
SDK 重试与错误行为参照
[OpenAI Python SDK 官方说明](https://github.com/openai/openai-python)。
