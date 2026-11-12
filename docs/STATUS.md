# 当前状态（2026-11-12）

Serve总目标仍为 **active**。按[完整计划](SERVE_MASTER_PLAN_2026-11-08.md)
和[逐项任务表](serve-master-goals-v2.json)推进全部33项任务；阶段验证与
发行是实施边界，不能替代剩余能力。非性能改动仅做受影响行为的针对性
验证和必要业务短验，长稳实跑由用户负责。

## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev80；服务包 `4263d0c36088598a69bceaafbe8e0bc7b79eb9c051dc3be2a6de4be3f10332bb` |
| Generation | `407157c6a754106060e634336a93a99d8883906d6ea8c2e4dd1dd30bb523f9e2` |
| 引擎 | `18c44ab1f301a0f7f69775f815295e649ecd8bbff8918b2de0a795cfa63cc9f8` |
| 引擎发布记录 | `12df970750d3d42aa0c5cdd89375ed9f50946585d989c07e5e08f901fea0b8ee` |
| Guardian | `383bcc63b26fe0911d1d99b455c1b4effc39b6f813e147a9cfbfb6bce5bf11dc` |
| 服务状态 | `STOPPED`；原生外层监护已配置enabled，累计启动0次 |
| 默认容量 | 单流、ctx8192、prefill2048、输出256、MTP k=3；inflight8/queue2 |
| 版本回退 | 实际已验同应用能力v12控制payload `c93e9714786643a76a933039bff0843df1d40030f2669085f6041af8db0e1ae7` |
| 外部依赖 | 默认原生入口及自监护不要求nginx/systemd；宿主集成、TLS/CORS等可选配置 |

[发行结果与回退记录](evidence/serve-refusal-delivery-20261112/README.md)
包含17项实际发布/回退/再次发布检查。原核心、Key/配额及outer状态
保持；profile仅资格身份及revision更新，预算/时钟/历史不变。
新版本周期保留前驱及原结果；没有新模型启动或开机项安装。
自动回退保持现代原生固定根；人工整根回退的旧入口能力单独记录。
工作分支已推送，**本轮尚未合入main**；主线交付仍属于Q02。

## 最近完成的实现和验证

- A11公开管理与内容读取已接通15条原生操作，统一schema/原生入口/
  客户端RPM及独立取消容量已接线；[12项C++和3项SDK/协议通过](evidence/serve-vector-api-cpu-20261112/README.md)。
  官方JS直连与Python经原生TLS均验证真实文件/索引文本，向量为CPU
  数学夹具；原失败保留，仅重验受影响项。实际search/Responses继续
  实施，0GPU，默认rev80保持。统一应用schema遗漏的Chat refusal
  类型也已同步，2项复用既有真实JSON/SSE检查通过。

- A11文件入库批次的持久接口已成组编码，
  [17项CPU首过](evidence/serve-vector-batches-cpu-20261112/README.md)：2000文件
  合同、原子计数/取消及批次分页；删除中的旧执行保留至实际清理，
  重挂载不能串用。继续公开HTTP/SDK/search/Responses接线。

- A11持久入库调度与真实ChatServer模型回调已接线，
  [18 项 CPU 首过](evidence/serve-vector-jobs-cpu-20261112/README.md)，实际引擎零警告构建：
  有界公平队列、原进程确认退出才恢复、取消持久化及实际清理后闭合，
  未知提交只核对原索引。辅助健康独立于核心readiness；公开API/
  SDK/Responses工具循环继续实施，默认rev80尚未变更。

- A11入库的客户端 TPM/并发许可与 BF16 输出适配已编码，
  [14 项针对性 CPU 通过](evidence/serve-vector-quota-cpu-20261112/README.md)。
  内部任务不重复外部 RPM，异常保留消费、未知用量保守结算；
  原生调度器与实际 GPU 回调继续接线，默认 rev80 保持。
- A11真实Files/隔离文档提取与tokenizer接线已编码，[11项CPU首过](evidence/serve-vector-ingestion-cpu-20261112/README.md)；
  原文件与NFC文本分别绑定，完整分块/EOS预算先准入，失败保留实际
  用量与未知提交身份。生产控制器/API/模型回调仍继续接线。
- A11持久父对象/文件关联、原子计数与索引发布、过期/回收和有界
  claim恢复状态已编码，[19项CPU已通过](evidence/serve-vector-stores-cpu-20261112/README.md)。实际提取/embedding
  执行器、HTTP/SDK及Responses仍继续实施，当前未增公开能力。
- A11原生资料检索开始成组实现：官方合同核对完成，参数、精确属性
  过滤及有界cosine/BM25排序16项CPU首过（0.026秒）；持久索引、
  API和Responses执行循环接着实现，尚未声明生产file_search可用。
  [实施与验收计划](SERVE_NATIVE_FILE_SEARCH_2026-11-12.md)，默认rev80保持。
- A11索引构建/持久化成组编码：实际token分块保UTF-8、逐块EOS和
  已消费用量，索引绑定源文件及embedding/tokenizer身份，复用不可变
  快照持久化；9新+2资产直接回归CPU首过（0.984秒）。尚未接公开存储API和
  Responses执行器，下一步继续对象管理/任务恢复/工具闭环。
- 原生外层恢复、真实资源域采集与版本回退组合已验并发行。真实root
  死亡后清树重建、未知配额保守窗口、单次文档工具、JSON/SSE与原存储
  Chat保持通过。[业务记录](evidence/serve-outer-resource-business-20261112/README.md)：
  三轮累计6启动/8Chat/1探测，严格租约283.572秒，原失败全部保留。
- 运行入口降权现在仅保留原用户已有video/render组；真实设备权限
  缺陷已修复，实际模型启动通过。[CPU与原失败](evidence/serve-runtime-identity-cpu-20261112/README.md)。
- 冷准入验收的root入口现在在永久降权子进程中核验私有策略；
  [5项CPU通过](evidence/serve-cold-owner-cpu-20261112/README.md)，未操作GPU或设备规则。
- A07模型生成拒绝已发行：Chat/Responses JSON/SSE、约束独立分支、
  真实概率、对象/历史与Python/JS客户端均接通。80项原生+19项适配
  [CPU](evidence/serve-refusal-cpu-20261112/README.md)、6请求
  [真实业务](evidence/serve-refusal-business-20261112/README.md)、
  [主/轻量资格](evidence/serve-refusal-profiles-20261112/README.md)通过。
- 新能力升级及回退已发行：修复旧启动自检对合法refusal的TypeError，
  [受监护及真实版本回退](evidence/serve-refusal-managed-business-20261112/README.md)
  累计含失败4启动/7Chat/1探测/248.927秒；拒绝对象、历史和预算保持。
  [发布副本归档](evidence/serve-publication-archive-20261112/README.md)保留
  原包及证据，0文件删除；发行后活动池31/32，归档2代可核验恢复。
- 已发布资源域约束覆盖内核memcg计量及独立物理可用内存观察，
  **没有声称实现统一DRAM/GPU物理硬上限**；R08仍须解决剩余边界。

## 全部任务进展与剩余缺口

详细实现、来源和证据以[任务表](serve-master-goals-v2.json)为准。
下列合并行只用于导航，不改变各任务的完成条件。

| 任务 | 当前状态与仍需完成的内容 |
|---|---|
| G00 | 推理/应用服务之外的组织、账单、训练管理范围待用户决定；不阻塞已确定任务 |
| A01/A02 | 已交付多指令、name、refusal历史等输入能力；剩余字段/模板及媒体、工具组合待逐项闭合 |
| A03/A04 | 采样/penalty/logprobs/multi-choice/prediction与工具选择、增量流、legacy/custom工具已验交付 |
| A05 | 丰富JSON Schema及工具/媒体组合已交付；A10媒体身份与状态缓存的组合尚待实现 |
| A06 | Completions/Models已验交付 |
| A07 | Responses核心、状态、reasoning/summary/encryption/compaction等已交付；原生refusal生产/SSE及恢复已发行；内建工具及媒体items、async/caller/namespace/output_schema等仍须实现 |
| A08 | 持久Chat/Responses、Conversations、background/cancel等已交付 |
| A09 | Files/Uploads/Batch已有实际交付；剩余8类endpoint后端及对应SDK/发行仍须与A10完成 |
| A10 | 原生embedding检索及moderation等已有交付；audio/realtime/image generation/edit/video等后端、媒体缓存及组合仍待实现 |
| A11 | 内建工具/MCP及项目资料检索执行边界待实现；权限、超时、取消、输出上限与完整事件是验收要求 |
| A12 | 官方Python/JS及现有客户端的最终兼容、错误/流、迁移和发行矩阵待闭合 |
| N01—N07 | 原生接入、TLS/证书、CORS、Key/配额治理、HTTP、运维与发行入口均已验交付；外部前置保持可选 |
| R01—R06 | rev80保留已验隔离与恢复；新能力profile升级及同能力回退已验发行，原恢复预算保持；后续组合继续见R07—R10 |
| R07 | NVML失能冷准入代码/CPU验证已推进；实际设备独占资格、冷业务与组合验收未完成 |
| R08 | 原生资源域、压力观察与实际恢复组合已推进；物理内存硬边界及完整资源验收仍须完成 |
| R09 | 正常NVML下原生外层/资源/版本恢复已验发行；冷准入及宿主故障组合仍须闭合 |
| R10 | 视觉与恢复、降级、工具/schema组合待实现和验证 |
| Q01—Q03 | 跨接口真实资料查询闭环、最终可用主线发行、全任务核对和长稳交接仍待完成 |

## 接下来

1. 基于已修复入口与实际rev80身份重建R07冻结计划。原v1 plan-v2
   因入口缺陷及来源/默认变化而失效，不能执行。涉及nvargus/jtop的
   维护问题仍待回复，不以等待阻塞独立开发，不擅自干预其他服务。
2. [能力升级及恢复连续性](SERVE_CAPABILITY_UPGRADE_2026-11-12.md)已发行，继续A11项目资料查询的服务端工具执行/Responses事件，以及A10所需后端；先按官方合同
   和代码明确缺口，再成组实现，做针对性CPU及必要真实业务。
3. R08/R09剩余项与R10组合持续推进；各阶段可回退交付后继续下一项，
   全部计划完成前不结束总目标。

## 工作区、证据与保留候选

当前有效开发入口为主工作区的 `codex/serve-resilience-cd`。
隔离分支 `codex/serve-outer-recovery` 固定在 `38b0d29f`，已被当前工作
分支完整包含；其工作区与 `.q4t-work` 原路径继续保留，因为已发布包
与验收原件依赖这些路径。此时不删除工作区或证据。

视觉行最大值候选仍保留，未追加该候选GPU测试；范围和阻断记录见
[视觉补验结果](VISION_ROWMAX_COMPLETION_RESULT_2026-11-07.md)。
长稳实跑交给用户，短验不替代长稳结论。

此前STATUS五千多行原件完整保存在
[历史状态快照](HISTORY_STATUS_2026-11-12.md)，SHA256：
`40ae237add2d4203a4722e722a6cbfcf1925e00e7c5724e128f7bb84b15deae0`。历史结论不代表当前默认或剩余任务已完成；开发过程记录见
[当日日志](log/2026-11-12.md)。
