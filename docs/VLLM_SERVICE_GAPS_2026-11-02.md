# vLLM 对外服务能力对照与遗漏清单

用户要求（2026-11-02）：参考 vLLM 的整体服务能力，发现类似“有生成
引擎却缺工具响应”的应用层遗漏。与[主兼容矩阵](APPLICATION_SERVICE.md)
一起维护；不能仅用功能数量衡量成熟度。

工具控制 / 结构化输出的
[实现合同](STRUCTURED_OUTPUT_DEVELOPMENT_2026-11-02.md)已明确，后续已完成
[有界验收与交付](STRUCTURED_OUTPUT_RESULT_2026-11-02.md)。
下表已交付状态不因源码开发自动升级。

2026-11-03 [SRV-12本地运营限定接受](SERVE_OPERATIONS_RESULT_2026-11-03.md)：
17告警/只读诊断/匿名投影与留存、运行手册；CPU/真实资料工具、诊断
与临时Prometheus健康/停机检查通过。owner规则按metrics自身占普通
连接1修订，原运行与CPU增量分列。通知与原始日志轮转未上线，下一项
先补metrics/config/capabilities在普通连接饱和时的可用性。

后续[serve 全链路审计](SERVE_COMPLETENESS_AUDIT_2026-11-02.md)已覆盖
审计起点6路由/35字段（当前10路由/38字段）并复现输入与取消的嵌套合同缺口，补充可空值、
监控口径、流式存活和编译资源边界；后续输入A组与F5已修，F6独立
准入、exec隔离、编译/匹配超时与2GiB地址空间限额已补；整机RAM和
独立RSS保证仍不具备，不能沿用本表的
单项成功结论把组合或未审形状标为通过。

2026-11-03 [统一内存准入限定交付](SERVE_MEMORY_ADMISSION_RESULT_2026-11-03.md)：
增加全局MemAvailable门限/观测失效拒绝与恢复滞回，ready/config/固定
指标同步；实际高floor提前拒绝、默认普通/strict/真实工具通过。提前
准入不保留RAM，整机硬保护仍待办。

2026-11-03 [同PID压力恢复与64轮混合业务限定接受](SERVE_PRESSURE_RESULT_2026-11-03.md)：
326生成尝试/4负例/1启动和65预检首轮符合预期；实际4.875GiB匿名压力
关闭/排队回收/滞回/恢复，64轮普通/strict/真实工具与资源包络通过，
worker196/196reap。补同模型动态恢复和有界持续负载证据，不证明小时/
天级长稳、物理RAM硬保护或实际部署网关/租户策略。

2026-11-03 [阶段A结果与明确限制](SERVE_STAGE_A_RESULT_2026-11-03.md)：
请求身份、就绪/控制入口、HTTP/SSE及普通流背压限定交付。保留全部
失败和未观察项，未把这些结果扩展到所有严格流组合或长prefill。

## 证据口径

只读本地 vLLM `67111973eeb104e83bc6707a2d0f139737b711a0`，入口为
`reference/vllm/vllm/entrypoints/openai/` 和 `entrypoints/serve/`。
同时核对当前官方文档（访问日期 2026-11-02）；未更新、构建或启动
reference，未把 stable 网页当作本地固定 commit 的相同版本。
本项目盘点起点 460ffd0，下表已同步f1133bcc输入/编译隔离及serve阶段A/B1状态；历史
差异与具体失败另列，未验部分不标成熟。

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
| 工具选择控制 | auto/none/required/named；约束后端参与保证 | 已验四种模式，named 恰好一次；固定 XGrammar 约束 | B 首组已交付；受约束仅 parsed 文本 |
| 禁止并行工具 | parallel_tool_calls=false 对输出作限制 | 已验 false 最多一次；true 不代表客户端并发执行 | B 首组已交付，完整批次验证后回填 |
| 工具参数严格性 | strict / structural tags 受模型、parser、配置约束 | 有限 q4t-json-schema-v1，掩码＋最终独立验证；非完整 schema | B 首组已交付；未知 keyword 拒绝 |
| JSON / schema / grammar | structured_outputs、response_format | json_object / 有限 json_schema 已验；未暴露任意 grammar | B 首组已交付；新路径不组合缓存 / 媒体 |
| 文本停止条件 | stop 字符串 / token IDs、终态与 stop_reason | 仅 EOS / token预算 / 容量停止；有效 stop 拒绝 | B：跨 token、跨块、与工具标记组合验 |
| 输出预算 | max_completion_tokens / legacy max_tokens | 首批已验两字段共用预算 | A 已交付：双非空拒绝 |
| 消息角色 | 接口schema接收OpenAI角色，最终受模板支持限制 | SRV-04已验单条开头developer替代system；混用/重复/错位拒绝 | [B1限定交付](SERVE_STAGE_B1_RESULT_2026-11-03.md)：原生字节/ID及SDK工具闭环 |
| 消息 name / refusal / audio | schema有相应字段，具体模型路径决定语义 | 非null仍拒绝；B1确认模型不表达name并定位具体字段 | B1拒绝合同已验；有具体模板语义才另行支持 |
| 嵌套输入校验 | 独立协议类型与 validation handlers | 工具定义 / 历史 / cancel 已补共用严格校验，真实 JSON/SSE 工具闭环通过 | [输入合同 A 组已交付](SERVE_INPUT_CONTRACT_2026-11-02.md)：F1/F2/F4 |
| 推理输出字段 | 当前官方 / 固定源码为 reasoning | 05a已交付：默认reasoning_content，显式选reasoning；历史双字段冲突校验 | B：两字段JSON/SSE及真实工具回填已验；非Responses |
| 推理隐藏与预算 | include_reasoning / thinking_token_budget 等扩展 | 05a include_reasoning隐藏已验；仍无独立推理预算 | B：05b继续真实选词与强制闭合，隐藏不减少usage |
| SSE 创建时间 | serving 对一请求持同一 created | 首批已统一同一请求各块时间 | A 已交付 |
| SSE 工具参数增量 | parser 维护每调用 index/ID/参数增量 | 普通路径完整调用后发布；受约束路径完整响应验证后发布 | B：非逐 token 参数流，保留时延边界 |
| SSE 用量与中断 | usage tail、可选持续统计；断连不保证尾帧 | 已验尾帧/心跳、普通流和固定12443字节严格复制输出的实际背压；05b推理usage已交付 | [严格复制结果](SERVE_STRICT_TRANSPORT_RESULT_2026-11-03.md)限定通过；任意生成/网络与长稳仍需证明 |
| 错误语义 | protocol/exception handlers 分类与封套 | 首批已统一 JSON/SSE 分类与封套 | A 已交付；Q4T code 不等同所有外部错误语义 |
| 关联请求 ID | middleware/x_request_id.py；可选请求 / 响应头 | X-Request-Id接收/随机生成/回传，诊断关联，和JSON取消ID分离 | SRV-01限定交付；仍不提供幂等 |
| 幂等 / 自动重试 | 不能从 request ID 或 SDK 重试推断幂等执行 | 活跃 ID 去重，无持久幂等存储 | B：本轮禁自动重放，恢复判定已具备 |
| 模型发现 | models/api_router.py，served names / adapter 视配置 | 单模型 models；无详情、别名策略 | B：客户端发现与错误模型合同 |
| Tokenizer / 渲染预检 | tokenize/api_router.py、renderer APIs | 有离线 tokenizer / template 工具，无 HTTP 预检 | B：业务预预算，避免长请求到推理时才失败 |
| 默认生成配置 | generation_config 与 serving 配置 | greedy 固定、若干中性参数兼容 | B：明确 effective defaults 与配置来源 |
| 多 choice / logprobs / sampling | 相应生成参数与响应结构 | n=1、greedy，logprobs 拒绝 | C：需要底层支持，不是 HTTP 字段补丁 |
| 会话与 Responses | 独立 responses 路由 / stateful 路径，受配置限制 | 只支持 Chat；session cache 是同进程 KV | B：应用持久会话先于新增一套 API |
| 真实工具执行 | Chat function 调用通常由应用执行与回填 | 首批真实发布 / 历史验收 / 不存在资料查询已验 | A/P2 已交付有界只读链路；自主广泛检索未验 |
| 取消 / 断连 / 关闭 | serving 检查断连并传播 abort；生命周期管理 | 已有取消、deadline、断连、shutdown 与槽回收 | 保留证据；不是新的缺失 |
| 压力与配额 | 调度、准入与服务配置；不等于完整租户计费平台 | 连接 / body / chat / slot 上限已具备，无租户配额 | B/C：先错误可观测，再业务配额 |
| 健康与观测 | health/load/version、Prometheus、请求 metrics | F5指标、livez/readyz/排空与4控制连接；01b固定失败指标；12告警/只读身份诊断/手册与有界实际监控已验 | 12b先补观测端点饱和隔离；未分类慢头洪泛、实际通知/日志轮转与长稳仍未证明 |
| 鉴权 / CORS / TLS | middleware Bearer/CORS；服务 TLS 配置 | 原生未提供；现有 Nginx 示例未含完整鉴权配置 | C：在真实入口配置并验收，不能认为传了 api_key 就鉴权 |
| HTTP 媒体类型 / 方法 | 框架路由与 JSON request validation | application/json UTF-8、405＋Allow、query400、GET body400已验 | SRV-03限定交付；仍禁chunked上传/连接复用 |
| OpenAPI / SDK | 协议类型生成文档、官方 SDK 接入示例 | A响应/B1消息schema子集、自有客户端及Python SDK 3.42.1限定互通已验 | SRV-08继续完整输入协议/JS等矩阵，不外推其他SDK |
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

1. 工具选择控制 + 有限结构化输出已完成首组有界交付；更广 schema、
   并发、逐 token 结构化流及受约束加速另行冻结。
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

首批交付更新：上表从 460ffd0 盘点基线更新至当前交付；token 预算别名、错误
封套与类型、SSE created、已确认的嵌套消息语义拒绝、真实项目读取
已在 c3772d11 验证交付。Python SDK 3.42.1 的限定互通已通过，
仍不能提升为所有 SDK / 字段全兼容。轮次预算与监控环境两次实际
失败、明确复用的证据和剩余质量边界见
[首批结果](APPLICATION_SERVICE_RESULT_2026-11-02.md)。
