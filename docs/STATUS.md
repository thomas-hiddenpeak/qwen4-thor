# 当前状态（2026-11-12）

Serve总目标仍为 **active**。按[完整计划](SERVE_MASTER_PLAN_2026-11-08.md)
和[逐项任务表](serve-master-goals-v2.json)推进全部33项任务；阶段验证与
发行是实施边界，不能替代剩余能力。非性能改动仅做受影响行为的针对性
验证和必要业务短验，长稳实跑由用户负责。

## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev72；服务包 `5468f36a15f3303aa9360b313794459e09341a137001e297dbbc18b615ead2d8` |
| Generation | `a9713e3917d0d8e5d25c042f28ed564f6e379750656596d65d38700eb6e7cab2` |
| 引擎 | `96577e45d40f60211bc2915da29254e5ee0c8df63dd00e1356e4ceacafb7003a` |
| 引擎发布记录 | `c527fa7d589a9bc8a77dd3dc47558a3aa8122f818d1d55424626a4a4137c89bc` |
| Guardian | `383bcc63b26fe0911d1d99b455c1b4effc39b6f813e147a9cfbfb6bce5bf11dc` |
| 服务状态 | `STOPPED`；原生外层监护已配置enabled，累计启动0次 |
| 默认容量 | 单流、ctx8192、prefill2048、输出256、MTP k=3；inflight8/queue2 |
| 版本回退 | 实际已验v12资源适配包 `5993494254afc76d3963be42cd3adcf10729068a376a07e4786fd2bbc79e938b` |
| 外部依赖 | 默认原生入口及自监护不要求nginx/systemd；宿主集成、TLS/CORS等可选配置 |

[发行结果与回退记录](evidence/serve-outer-resource-delivery-20261112/README.md)
包含完整包路径、身份、原状态保护及19项实际发布/回退/再次发布检查。
原核心、Key/配额和profile账本保持；没有新模型启动或开机项安装。
工作分支已推送，**本轮尚未合入main**；主线交付仍属于Q02。

## 最近完成的实现和验证

- 原生外层恢复、真实资源域采集与版本回退组合已验并发行。真实root
  死亡后清树重建、未知配额保守窗口、单次文档工具、JSON/SSE与原存储
  Chat保持通过。[业务记录](evidence/serve-outer-resource-business-20261112/README.md)：
  三轮累计6启动/8Chat/1探测，严格租约283.572秒，原失败全部保留。
- 运行入口降权现在仅保留原用户已有video/render组；真实设备权限
  缺陷已修复，实际模型启动通过。[CPU与原失败](evidence/serve-runtime-identity-cpu-20261112/README.md)。
- 冷准入验收的root入口现在在永久降权子进程中核验私有策略；
  [5项CPU通过](evidence/serve-cold-owner-cpu-20261112/README.md)，未操作GPU或设备规则。
- A07拒绝输出已接通模型帧、约束分支及独立receipt、Chat/Responses
  JSON/SSE、真实logprobs和状态回填。80项原生及19项协议/SDK/客户端
  针对性检查通过，[CPU证据](evidence/serve-refusal-cpu-20261112/README.md)
  保留原失败与修复。[真实业务](evidence/serve-refusal-business-20261112/README.md)
  1启动/6生成/60.217秒通过，含模型拒绝与实际工具闭环；可回退发行
  待完成，实际默认仍为上表rev72。
  [主/轻量实际资格](evidence/serve-refusal-profiles-20261112/README.md)已通过
  2启动/6Chat/2embedding/91.950秒并封包，[受监护及版本回退](evidence/serve-refusal-managed-business-20261112/README.md)已通过，发行继续。
  同能力独立回退包已准备；[受监护驱动5项CPU边界](evidence/serve-feature-driver-cpu-20261112/README.md)已通过，
  首轮发现旧自检把合法refusal的null正文送入JSON解析，已修复并通过3项CPU与真实响应回放；[原失败](evidence/serve-refusal-selfcheck-failure-20261112/README.md)保留，补验累计上限4启动/7Chat/1探测/1200秒，不重跑模型全矩阵。
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
| A07 | Responses核心、状态、reasoning/summary/encryption/compaction等已交付；原生refusal生产/SSE及恢复已验待发行；内建工具及媒体items、async/caller/namespace/output_schema等仍须实现 |
| A08 | 持久Chat/Responses、Conversations、background/cancel等已交付 |
| A09 | Files/Uploads/Batch已有实际交付；剩余8类endpoint后端及对应SDK/发行仍须与A10完成 |
| A10 | 原生embedding检索及moderation等已有交付；audio/realtime/image generation/edit/video等后端、媒体缓存及组合仍待实现 |
| A11 | 内建工具/MCP及项目资料检索执行边界待实现；权限、超时、取消、输出上限与完整事件是验收要求 |
| A12 | 官方Python/JS及现有客户端的最终兼容、错误/流、迁移和发行矩阵待闭合 |
| N01—N07 | 原生接入、TLS/证书、CORS、Key/配额治理、HTTP、运维与发行入口均已验交付；外部前置保持可选 |
| R01—R06 | rev72的隔离与恢复已验交付；新API能力的profile升级/回退连续性正在补齐，16项迁移及3项驱动CPU、新主/轻量实际资格通过；新回退组合已验、发行待完成 |
| R07 | NVML失能冷准入代码/CPU验证已推进；实际设备独占资格、冷业务与组合验收未完成 |
| R08 | 原生资源域、压力观察与实际恢复组合已推进；物理内存硬边界及完整资源验收仍须完成 |
| R09 | 正常NVML下原生外层/资源/版本恢复已验发行；冷准入及宿主故障组合仍须闭合 |
| R10 | 视觉与恢复、降级、工具/schema组合待实现和验证 |
| Q01—Q03 | 跨接口真实资料查询闭环、最终可用主线发行、全任务核对和长稳交接仍待完成 |

## 接下来

1. 基于已修复入口与实际rev72身份重建R07冻结计划。原v1 plan-v2
   因入口缺陷及来源/默认变化而失效，不能执行。涉及nvargus/jtop的
   维护问题仍待回复，不以等待阻塞独立开发，不擅自干预其他服务。
2. 先完成[能力升级及恢复连续性](SERVE_CAPABILITY_UPGRADE_2026-11-12.md)的新包资格/发行，保持原生接入和恢复能力。继续推进[A07模型拒绝输出合同](SERVE_REFUSAL_OUTPUT_2026-11-12.md)、A11工具执行及A10所需后端；先按官方合同
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
