# 当前协议清单与总任务再核对

2026-11-16，源码基线`a362d68d`，实际默认rev150。此次只更新清单和
实施追踪；运行时未改，0GPU。目标仍是完成全部33项，已交付阶段不
替代未实现接口。原2026-11-08官方协议快照保持，不追改历史记录。

## 发现与修正

核对45个Chat字段、21个Completions字段、74条后端路由和123条原生
入口路由（包含OPTIONS）。清单的来源哈希保护只能发现源码漂移，
不能证明描述仍准确，更不能证明功能完整。旧条目把Models别名、
schema-v2、持久Chat/Conversations、Files/Uploads/Batch、原生embedding、
检索和MCP等已交付能力仍写作开发中，已按代码和实际发行记录修正。

OPTIONS明确为CORS预检，不将它描述成实际执行对象操作或模型。
Models DELETE对只读基础模型403仍是正确权限合同；没有伪造删除。
Completions改为逐字段写出prompt/token批量、echo/FIM、best_of、
logprobs、预算和流式语义，不再用通用“实现中”占位。

原日期命名的review/development条目保留为历史；当前事实读取
`current_surface_review`与当前fields/routes。`semantics_certified:false`
继续保留：人工核对这些描述不等于全面协议认证，也不新增运行时验收。

## 仍须实现的具体合同

| 工作 | 当前源码/证据 | 必须继续的实现 |
|---|---|---|
| A01 Chat顶层字段 | `chat_contract.cpp`与冻结37字段参考相比，仍缺8个 | `audio`、`modalities`、`moderation`、`prompt_cache_options`、`safety_identifier`、`service_tier`、`verbosity`、`web_search_options`；复用Responses已有组件并完成实际行为，不能只加allowlist |
| A01/A02嵌套合同 | Chat `stream_options`仅include_usage；模板effort仅low/medium/xhigh | 接通include_obfuscation，核对null/缺省及完整effort映射；Responses已有更广枚举和原生执行，不应各入口永久分叉 |
| A02消息历史 | 角色/name/refusal/函数历史已交付；`audio`、`annotations`非null仍被拒绝 | 保留标准历史信息、绑定真实后端与输出，补当前模板/存储/SDK往返 |
| A05/A10组合 | `server_options.cpp`拒绝allow_media与状态cache共用；`text_stop.cpp`拒绝约束/私有状态cache组合 | 实际媒体身份、MRoPE和状态续接；停止/终态/缓存恢复须一致，不能以当前拒绝当成完成 |
| A07/A11工具与items | namespace/custom/async/discovery/programmatic/MCP、真实检索已交付 | 剩余标准输出生产者、内建工具和程序调用组合；云适配须真实账户权限/分页/内容验收 |
| A09/A10后端 | Batch接受8种端点名，已验Chat/Completions/Responses/Embeddings/Moderations | images/generations、images/edits、videos仍缺实际后端；音频/Realtime及剩余视觉格式也必须逐后端落实，不输出假结果 |
| A12客户端 | 多组官方Python/JS已有真实业务，整任务不能仍写planned | 完整对应能力矩阵、文件/媒体/持久工具及错误/流式组合、发布消费者和回退；复用身份未变证据 |
| R07 | 冷启动内核隔离/issuer/watcher与组合CPU已过 | 更新过时v1候选；实际维护窗口中的NVML失能冷启动、独占证据和组合发行 |
| R08 | 实际正常NVML资源域/外层恢复已交付 | 统一DRAM硬边界平台实证、冷启动/压力组合；不能把CPU memcg限额当GPU物理RAM限额 |
| R09 | 正常NVML外层/资源/版本恢复已交付 | 冷启动及宿主启动组合；保留显式STOPPED、持久预算和当前自动恢复根，补整根回退缺outer的组合 |
| G00 | 组织/账单/训练管理范围尚无用户决定 | 保持未决，不删项；不阻塞已确定推理与应用服务实现 |

这8个顶层字段来自已冻结的官方快照，不是当前网页的全量版本声明。
某字段在Responses已实现，不代表Chat同名行为自动成立；反之，旧清单
遗漏也不能把已有实现在所有入口重复开发一遍。

## 总任务状态与接续

33项均保留required。A03/A04/A06/A08、N01—N07、R01—R06/R10已有各自
通过记录；这些记录按精确身份复用。A01/A02/A05/A07/A09/A10/A11/A12、
R07/R08/R09、Q01/Q02/Q03和G00仍有剩余工作。当前Q02已发布rev150，
PR30主线状态需以实际远端回执更新，不能由本地发行推定已合并。

接下来先补可复用现有实现的Chat合同差异，再推进媒体缓存、剩余
工具/后端和客户端矩阵；资源/冷启动维护依赖单列并继续独立工作。
每组先冻结针对性CPU与必要业务短验，保持非性能改动不跑完整性能
或全模型正确性矩阵。全部计划实现、验证和交付之前总目标不完成。

当前可用身份见[STATUS](STATUS.md)，详细任务见
[机器可读总表](serve-master-goals-v2.json)。本次静态检查和逐行变更记录
见[审计证据](evidence/serve-surface-reaudit-20261116/README.md)。
