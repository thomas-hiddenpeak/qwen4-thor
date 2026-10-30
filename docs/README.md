# docs/ — 开发记录文档体系

现行规范：[统一RAM与SSD offload合同](SSD_OFFLOAD_CONTRACT.md)。
阶段收尾：[五个参考项目成果、已吸收特性与重启条件](REFERENCE_LEARNING_CLOSEOUT_2026-10-29.md)。
实际接入：[1长主agent＋3子代理，suffix与两级缓存完整示例](AGENT_CACHE_INTEGRATION.md)。
最新专项：[main复审四项修复交付、证据与回退](MAIN_REAUDIT_FIX_RESULT_2026-10-31.md)；
[修复范围与冻结预算](MAIN_REAUDIT_FIX_PLAN_2026-10-31.md)；
[main修复后复审：4项P2既有遗漏与回归检查](MAIN_REAUDIT_2026-10-31.md)；
[审计8项P2主线合入与分支清理](AUDIT_P2_RESULT_2026-10-31.md#主线合入2026-10-31)；
[全部交付、证据与回退](AUDIT_P2_RESULT_2026-10-31.md)；
[权重/SSD驻留修复结果](AUDIT_P2_STAGE1_RESULT_2026-10-31.md)；
[容量/监督修复结果](AUDIT_P2_STAGE2_RESULT_2026-10-31.md)；
[三阶段修复范围与第一阶段合同](AUDIT_P2_FIX_PLAN_2026-10-31.md)；
[项目全面审计：8项P2、复现与覆盖边界](PROJECT_AUDIT_2026-10-31.md)；
[Serve两项审计收尾主线合入与分支清理](SERVE_CLOSEOUT_RESULT_2026-10-31.md#主线合入2026-10-31)；
[Serve两项审计收尾结果与默认交付](SERVE_CLOSEOUT_RESULT_2026-10-31.md)；
[Serve响应头与有效私有缓存负向验收冻结范围](SERVE_CLOSEOUT_2026-10-31.md)；
[Serve可靠性主线合入与分支收尾](SERVE_RELIABILITY_2026-10-30.md#主线合入2026-10-31)；
[Serve可靠性结果、指标与回退](SERVE_RELIABILITY_2026-10-30.md)；
[Serve冻结范围与预算](SERVE_RELIABILITY_PLAN_2026-10-30.md)；
[私有会话增量保存独立交付、收益与回退](SESSION_INCREMENTAL_DELIVERED_2026-10-29.md)；
[独立交付冻结合同](SESSION_INCREMENTAL_DELIVERY_PLAN_2026-10-29.md)；
[原增量研究结果与限制](SESSION_INCREMENTAL_RESULT_2026-10-29.md)；
[会话缓存与prefix主线合入、接受及收尾](SESSION_PREFIX_MAIN_MERGE_2026-10-29.md)；
[显式公共种子验收及交付](PREFIX_SEED_RESULT_2026-10-28.md)；
[显式种子冻结方案](PREFIX_SEED_PLAN_2026-10-28.md)；
[跨会话prefix共享结果及NO_GO](PREFIX_CACHE_RESULT_2026-10-28.md)；
[可配置检查点与事前状态合同](PREFIX_CACHE_PLAN_2026-10-28.md)；
[完整会话缓存验收、交付与回退](SESSION_CACHE_RESULT_2026-10-27.md)；
[完整状态合同与有界方案](SESSION_CACHE_PLAN_2026-10-27.md)；
[会话缓存阶段过程](SESSION_CACHE_PROGRESS_2026-10-27.md)；
[suffix与长短agent主线合入/分支收尾](MTP_SUFFIX_MAIN_MERGE_2026-10-27.md)；
[长短agent已交付与使用/回退](MTP_SUFFIX_AGENT_DELIVERED_2026-10-27.md)；
[用户本项接受与健康预算](MTP_SUFFIX_AGENT_ACCEPTANCE_2026-10-27.md)；
[长短agent完整补测与原门槛判定](MTP_SUFFIX_AGENT_CONFIRM_RESULT_2026-10-27.md)；
[补齐事前方案](MTP_SUFFIX_AGENT_CONFIRM_PLAN_2026-10-27.md)；
[长短agent前轮NO_GO归档](MTP_SUFFIX_AGENT_RESULTS_2026-10-27.md)；
[长短agent事前方案](MTP_SUFFIX_AGENT_PLAN_2026-10-27.md)；
[固定k3 suffix正式交付](MTP_SUFFIX_DELIVERED_2026-10-27.md)；
[按需分类B验收及保护原值](MTP_SUFFIX_LAZY_ACCEPTANCE_2026-10-26.md)；
[因果A结果](MTP_SUFFIX_CAUSAL_RESULTS_2026-10-26.md)；
[因果消融与修复方案（已执行）](MTP_SUFFIX_CAUSAL_PLAN_2026-10-26.md)；
[200K/8K GPU对照与最终不交付决定](MTP_SUFFIX_FINAL_OBSERVATION_2026-10-26.md)；
[超限材料核对与ON复用](MTP_SUFFIX_LOG_REUSE_2026-10-26.md)；
[200K筛选采集与分析期限](MTP_SUFFIX_PROJECTED_200K_2026-10-26.md)；
[精确确认行筛选CPU核对](MTP_SUFFIX_ACK_PROJECTION_2026-10-26.md)；
[阻塞客户端观察与日志量定位](MTP_SUFFIX_CLIENT_OBSERVER_2026-10-26.md)；
[8K GPU区间与200K容量边界](MTP_SUFFIX_GPU_OBSERVATION_2026-10-26.md)；
[目标优先退出](MTP_SUFFIX_TARGET_FIRST_2026-10-26.md)；
[日志流CPU修复](MTP_SUFFIX_LOGSTREAM_2026-10-26.md)；
[流式GPU补测停止](MTP_SUFFIX_STREAM_PAIRS_2026-10-26.md)；
[GPU对照有界停止](MTP_SUFFIX_GPU_PAIRS_2026-10-26.md)；
[实际8K参数机制](MTP_SUFFIX_MODEL_LEDGER_2026-10-26.md)；
[回调身份与固定机制对照](MTP_SUFFIX_CALLBACK_PAIRING_2026-10-26.md)；
[生产链接依赖修复](MTP_SUFFIX_MEMSET_LINK_2026-10-26.md)；
[固定GEMM参数诊断](MTP_SUFFIX_MEMSET_PARAMETER_2026-10-26.md)；
[报告绑定修复与有限证据](MTP_SUFFIX_REPORT_BINDING_2026-10-26.md)；
[同步收尾验证](MTP_SUFFIX_SYNCHRONOUS_2026-10-26.md)；
[API自动收尾验证](MTP_SUFFIX_AUTOMATIC_2026-10-26.md)；
[新采集工具资格](MTP_SUFFIX_NSYS_265_2026-10-26.md)；
[采集合同与固定依赖候选](MTP_SUFFIX_COLLECTOR_DEPENDENCY_2026-10-26.md)；
[固定k3 suffix交付目标](MTP_SUFFIX_DELIVERY_2026-10-26.md)；
[fixture退出监控修复与Nsight补测](MTP_SUFFIX_NSYS_RERUN_2026-10-24.md)；
[Nsight收尾诊断](MTP_SUFFIX_NSYS_2026-10-24.md)；
[修复客户端监控后执行观测](MTP_SUFFIX_EXECUTION_RERUN_2026-10-24.md)；
[suffix执行区间观测](MTP_SUFFIX_EXECUTION_2026-10-24.md)；
前轮专项：[suffix修复监控后再验证](MTP_SUFFIX_ZERO_HIT_RERUN_2026-10-24.md)；
[suffix零命中成本诊断](MTP_SUFFIX_ZERO_HIT_2026-10-24.md)；
前轮结果：[suffix长匹配准入改进](MTP_SUFFIX_ADMISSION_2026-10-24.md)；
前轮结果：[数字格式suffix准入改进](MTP_SUFFIX_NUMERIC_GATE_2026-10-23.md)；
机制诊断见[8K回退离线取证](MTP_SUFFIX_8K_DIAGNOSIS_2026-10-23.md)；
原决定见[固定k3 suffix有界验收与NO_GO归档](MTP_SUFFIX_K3_2026-10-23.md)。
启动参数与三种解码配置见[根README使用说明](../README.md#使用)；
下一阶段是否可启动见[STATUS准入评估](STATUS.md#当前推进目标)。

本目录包含当前入口与按日期冻结的历史报告。先读[STATUS](STATUS.md)和
[EVALUATION](EVALUATION.md)；下表专题的“进行中/下一步/当前”仅代表成文
时的状态，不是当前任务队列或发布承诺。实现以src/代码为准。

| 文档 | 职责 | 更新时机 |
|---|---|---|
| [五个参考项目阶段收尾](REFERENCE_LEARNING_CLOSEOUT_2026-10-29.md) | 固定源码、已吸收能力、未移植项与需求触发条件 | 本轮完成；新证据成立后另立任务 |
| [显式公共种子交付](PREFIX_SEED_RESULT_2026-10-28.md) | 可配置公共边界、每请求单种子、子代理默认只读，1长＋3短两head收益 | 279HTTP/19服务/2fixture；6f37/5fdc交付，8K与32K冻结目标全部达成，统计非劣未建立 |
| [跨会话prefix共享结果](PREFIX_CACHE_RESULT_2026-10-28.md) | 完整状态共享、可配置检查点、两head净收益与写入成本 | 104HTTP/6服务/3fixture；功能合同通过，目标未全满足，NO_GO保持09fe |
| [完整会话缓存交付](SESSION_CACHE_RESULT_2026-10-27.md) | 同进程完整目标/draft状态SSD快照、跨槽恢复、两head净收益和五档原值 | 233HTTP/14服务/2状态fixture；09fe/5027交付，显式启用，总体非劣未建立 |
| [suffix阶段主线合入](MTP_SUFFIX_MAIN_MERGE_2026-10-27.md) | 58提交快进合入、160源码核验、旧tip/main保留锚点及分支清理 | main已推送，已交付程序与验收范围不变，工作区和证据保留 |
| [长短agent交付](MTP_SUFFIX_AGENT_DELIVERED_2026-10-27.md) | f2a6程序/发布元数据、1长＋3短用法、回退包与健康 | 用户接受实测收益/负点，2实际启动3HTTP通过；原优化FAIL保留，无常驻服务 |
| [长短agent本项接受](MTP_SUFFIX_AGENT_ACCEPTANCE_2026-10-27.md) | 用户原话、范围、原值、健康预算及失败出口 | 特定身份工程接受，不扩展通用容差或追认3%优化目标 |
| [长短agent完整判定](MTP_SUFFIX_AGENT_CONFIRM_RESULT_2026-10-27.md) | 补93HTTP累计320，19组保护输出全一致，4252监控全flush | 保留原门槛下优化/交付拒绝；其后用户接受及部署见新交付记录 |
| [长短agent并发结果](MTP_SUFFIX_AGENT_RESULTS_2026-10-27.md) | 227HTTP、两head长短重复对照、容量/分块prefill/32短请求准入 | subset未达3%decode目标；单流保护缺93请求、期限届满，NO_GO归档，90f保持 |
| [长短agent事前方案](MTP_SUFFIX_AGENT_PLAN_2026-10-27.md) | 208896+3×65536、prefill4096、两head有界矩阵 | 冻结记录；阶段已结束，不自动重启或扩样 |
| [固定k3 suffix交付](MTP_SUFFIX_DELIVERED_2026-10-27.md) | 显式启用90f、回退包、2启动3HTTP健康 | 有限工程接受并交付，统计非劣未建立，full默认不变 |
| [按需分类B验收](MTP_SUFFIX_LAZY_ACCEPTANCE_2026-10-26.md) | 226HTTP、两head重复收益与完整五档/普通decode | 四收益目标满足；保护原值及剩余风险公开，不扩样 |
| [因果A结果](MTP_SUFFIX_CAUSAL_RESULTS_2026-10-26.md) | 60HTTP、同路径/处理前负控、唯一B选择 | 删除全词表启动Decode；旧200K因果未确认 |
| [GPU对照有界停止](MTP_SUFFIX_GPU_PAIRS_2026-10-26.md) | 真实8K/200K四臂有限GPU比较 | CPU37通过；首8K日志超限ENV_ABORT，0有效capture，保护NO_GO保持 |
| [实际8K参数机制](MTP_SUFFIX_MODEL_LEDGER_2026-10-26.md) | 独立CUPTI参数观察及单次真实HTTP | CPU28/固定控制/实际8K通过；6381实参0，7902非零活动全覆盖，非性能验收 |
| [回调身份与固定机制对照](MTP_SUFFIX_CALLBACK_PAIRING_2026-10-26.md) | 共享回调ID/线程口径、原日志复用及条件Nsight | CPU23、原日志参数及单次Nsight局部机制有效；warning/全模型边界保留，0模型HTTP |
| [生产链接依赖修复](MTP_SUFFIX_MEMSET_LINK_2026-10-26.md) | 原HC对象及固定参数binary身份 | 唯一构建通过，CUPTI正常退出但原解析INVALID，后续只复用原日志修复 |
| [固定GEMM参数诊断](MTP_SUFFIX_MEMSET_PARAMETER_2026-10-26.md) | 独立CUPTI参数/条件Nsight与不可变日志前缀 | CPU55通过；两次构建后漏生产对象停止，0GPU/模型/HTTP；最小依赖修正待后续 |
| [报告绑定修复](MTP_SUFFIX_REPORT_BINDING_2026-10-26.md) | 同步屏障后唯一owned报告/SHA绑定 | CPU32及两真实控制绑定通过；warning门未过、早期日志收据审计FAIL并局部核对，0模型HTTP |
| [同步收尾验证](MTP_SUFFIX_SYNCHRONOUS_2026-10-26.md) | repeat:1:sync与目标成功返回收据 | CPU62；Generated文字门超时ENV_ABORT，具体修复见报告绑定阶段 |
| [API自动收尾验证](MTP_SUFFIX_AUTOMATIC_2026-10-26.md) | 自动API stop与回调完成确认 | CPU46通过；报告已生成但回调未闭合，ENV_ABORT/0HTTP，不交付 |
| [新采集工具资格](MTP_SUFFIX_NSYS_265_2026-10-26.md) | 固定2026.5.1.161、两控制及条件6HTTP | 首控制仍拒绝stop；ENV_ABORT，0模型HTTP，审计与负向归档 |
| [采集合同与固定依赖候选](MTP_SUFFIX_COLLECTOR_DEPENDENCY_2026-10-26.md) | 完整目标矩阵、官方合同因果修正、单一2026.5.1包 | 包身份准备证据；资格另见新阶段 |
| [固定k3 suffix交付目标](MTP_SUFFIX_DELIVERY_2026-10-26.md) | interactive采集工具与条件200K/8K GPU对照 | CPU32通过；首控制仍拒绝stop，ENV_ABORT，0模型HTTP，负向归档不交付 |
| [fixture退出监控修复与Nsight补测](MTP_SUFFIX_NSYS_RERUN_2026-10-24.md) | PF_EXITING有界pending、四收尾控制 | CPU97/原生64通过；11/11采样，第三组控制生命周期错误ENV_ABORT，0模型请求 |
| [Nsight收尾诊断](MTP_SUFFIX_NSYS_2026-10-24.md) | 已知工作、负控、memory coverage | 首fixture退出监控ENV_ABORT；目标warning未定，保持交付版 |
| [修复客户端监控后执行观测](MTP_SUFFIX_EXECUTION_RERUN_2026-10-24.md) | 正常/CPU/profile分阶段观测 | 37正常+6CPU局部有效；首profile完整性门拒绝，GPU矩阵未完成 |
| [suffix执行区间观测](MTP_SUFFIX_EXECUTION_2026-10-24.md) | prefill/神经草稿CPU双钟与NVTX | 4响应后监控ENV_ABORT；CPU/GPU定位未执行，保持交付版 |
| [suffix修复监控后再验证](MTP_SUFFIX_ZERO_HIT_RERUN_2026-10-24.md) | 正常收益/首long干预/分层计时 | 56HTTP/12启动有效；重复收益复现，根因未定，保持交付版 |
| [suffix零命中成本](MTP_SUFFIX_ZERO_HIT_2026-10-24.md) | TTFT前段/解码归因、同身份控制、只读遥测 | 冻结26HTTP诊断；候选需机制支持，全任务有界 |
| [suffix长匹配准入](MTP_SUFFIX_ADMISSION_2026-10-24.md) | 45K/8K步序原因、固定32-token门、两head重复保护 | 250HTTP完成；旧8K/45K额外工作消除、两head重复获益，耗时保护未闭合，NO_GO封存 |
| [数字格式suffix准入](MTP_SUFFIX_NUMERIC_GATE_2026-10-23.md) | 确定性过滤、两head配对块、新旧材料分离 | 238HTTP完成；重复收益保留，旧8K工作门与45K保护失败，NO_GO封存 |
| [8K suffix回退诊断](MTP_SUFFIX_8K_DIAGNOSIS_2026-10-23.md) | 数字短匹配重放、额外目标工作、注意力阈值边界 | 离线研究完成，0新增GPU/HTTP，NO_GO保持 |
| [固定k3 suffix草稿](MTP_SUFFIX_K3_2026-10-23.md) | 请求局部索引、独立状态合同、两head五档及成本 | NO_GO：4K/8K保护回退，默认保持 |
| [独立草稿head与SHA2主线合入](MTP_SUBSET_MAIN_MERGE_2026-10-23.md) | main身份、两级回退包、证据复用与分支整理 | 合入并推送，原工作区及证据保留 |
| [SHA2启动校验优化](SHA2_STARTUP_2026-10-23.md) | 完整hash硬件加速、两模式五档及回退 | 有界接受，启动中位减少25.276秒 |
| [独立BF16草稿head修正版验收](MTP_SUBSET_RESUME_2026-10-23.md) | 唯一65536行子集、直接合同、两模式五档及成本 | 有界接受，显式启用 |
| [GDS最小实验与C128历史数据](GDS_MINIMAL_EXPERIMENT_2026-10-21.md) | 严格/兼容实际路径、字节校验、库缓冲限制及历史prefill/decode | 实机诊断完成，未接受原生GDS或性能优化 |
| [SSD offload合同](SSD_OFFLOAD_CONTRACT.md) | SSD唯一容量后备、DRAM缓存/中转和逻辑/物理I/O定义 | 用户明确原则，当前规范 |
| [NVIDIA SSD直读评估](SSD_DIRECT_IO_EVALUATION_2026-10-21.md) | GDS平台资格、本机只读盘点、权重布局与工程优先级 | 原生GDS暂不接入，SSD主线继续 |
| [SSD offload前提对齐审计](SSD_OFFLOAD_ALIGNMENT_AUDIT_2026-10-21.md) | 近期代码/缓存条件与SSD目标对齐、后续优先级 | 只读审计完成，先核验SSD资源与读取约束 |
| [启动资源计划正式交付](STARTUP_DELIVERY_2026-10-21.md) | 正式运行时、实际默认与有界验收/部署 | 已交付默认5a456351，回退与证据封存 |
| [启动资源计划的临时锁页准备](STARTUP_CACHE_PREPARATION_2026-10-21.md) | 实际预热归属、默认MTP/重启/容量边界与回收 | 有界可靠性通过，默认保持 |
| [配置推导启动资源计划](STARTUP_RESOURCE_PLAN_2026-10-20.md) | 配置/形状、MTP启动资源与所有权合同 | 实现与早期失败历史；验收见续项 |
| [完整热启动设备预留原型](DEVICE_RESERVE_STARTUP_2026-10-20.md) | 真实启动清单预留、失败回收及完整热启动验证 | 固定配置两次通过，独立原型收尾、默认不变 |
| [完整热缓存物理回收时间轴](WARM_RECLAIM_TIMELINE_2026-10-20.md) | order9规整定位、唯一前置规整失败及原始证据 | 有界诊断完成，热启动未修复 |
| [首分叉局部重放](UPSTREAM_LOCAL_REPLAY_2026-10-20.md) | GEMV精确FP32、GDN前缀状态和单a干预通过；cuBLAS解释待闭合 | 调查进行中 |
| [上游首分叉捕获](UPSTREAM_FIRST_FORK_CAPTURE_2026-10-19.md) | layer0 a[43]同输入差异与精确点积，根因待闭合 | 调查进行中 |
| [真实文本MoE捕获与重放](MOE_REPLAY_REAL_INPUT_RESULT_2026-10-19.md) | 完整token身份、真实层输入、同形状逐位与T1/T4分项报告 | 诊断合同通过 |
| [主干MoE固定IDs诊断](MOE_REPLAY_A1_2026-10-19.md) | 独立入口、固定夹具、同形状逐位/当前分数语义及正常路径保护 | A1诊断交付 |
| [B1a默认入口部署](MTP_POSITION_B1A_DEPLOY_2026-10-18.md) | 已验证B部署、身份修正、A回退；服务保持停止 | 当前部署 |
| [B1a主线交付](MTP_POSITION_B1A_MERGE_2026-10-18.md) | 有限工程接受、主线合入、回退与部署边界 | 当前交付 |
| [B1a最终交付审查](MTP_POSITION_B1A_FINAL_REVIEW_2026-10-18.md) | 零采样最终审查、风险与有限工程接受依据 | 已采纳 |
| [简化改动规则草案](EVALUATION_SIMPLIFICATION_DRAFT_2026-10-18.md) | 原提案保留；已纳入EVALUATION | 已采纳 |
| [B1a条件判定补验卡](MTP_POSITION_B1A_DECISION_PLAN_2026-10-18.md) | 300请求及0.2%门禁已撤回；保留未执行历史 | 已撤回 |
| [B1a同期补验结果](MTP_POSITION_B1A_AB60_RESULT_2026-10-18.md) | 60请求通过、两模式五档同期实测与剩余判定工作 | 当前交付 |
| [B1a同期对照冻结卡](MTP_POSITION_B1A_AB60_PLAN_2026-10-18.md) | 60请求描述对照、证据增量、停止与未决归档；已执行 | 冻结依据 |
| [B1a基线后复审](MTP_POSITION_B1A_AFTER_BASELINE_2026-10-18.md) | 新基线的增量证据、剩余缺口与有界出口 | 当前审查 |
| [no-MTP基线实测](NO_MTP_BASELINE_RESULT_2026-10-18.md) | 41请求描述基线、两次启动差异与原始证据 | 当前交付 |
| [no-MTP有界基线设计](NO_MTP_BASELINE_BOUNDED_2026-10-18.md) | 41请求冻结设计；已执行，结果单独归档 | 本阶段 |
| [描述与代码核对](DESCRIPTION_AUDIT_2026-10-17.md) | README/CLI/架构差异与构建身份 | 描述变化时 |
| [工具与规则收敛](MAIN_CONVERGENCE_2026-10-17.md) | 主线交付边界、证据复用与阶段 | 当前交付 |
| [研究归档](ARCHIVED_RESEARCH.md) | 固定历史、失败证据及恢复入口 | 归档变化时 |
| [MTP数值收口](MTP_NUMERICAL_CLOSURE_2026-10-16.md) | 独立前缀合同、PLE上传修复与新运行时验收 | 当前阶段 |
| [MTP归并候选结果](MTP_MAINLINE_READY_2026-10-16.md) | 原冻结binary验收、五项历史失败与回退 | PLE修复前历史 |
| [MTP归并边界决策](MTP_MERGE_BOUNDARY_2026-10-15.md) | 两套MTP实现归并边界、策略A裁决与逐文件归并计划 | 2026-10-16恢复与共用合同补正 |
| [MTP基础修复交付](MTP_FOUNDATION_2026-10-08.md) | 基础修复、原跨模式失败与证据复用边界 | 2026-10-08历史交付 |
| [MTP主线交付结果](MTP_MAINLINE_RESULT_2026-10-09.md) | 已合入main的有限能力、验证归属、成本与回退点 | 2026-10-09交付完成 |
| [MTP主线交付条件](MTP_MAINLINE_READINESS_2026-10-09.md) | 入口与集成阻碍清单、修复及实际合入 | 2026-10-09交付完成 |
| [MTP sequential有限正确性验收](MTP_STRICT_RESULT_2026-10-09.md) | 独立正确性证据、原失败与五档成本 | 2026-10-09历史阶段 |
| [MTP严格greedy基线](MTP_STRICT_BASELINE_2026-10-09.md) | 显式sequential target接口、状态与验收边界 | 2026-10-09历史阶段 |
| [MTP阶段性转正计划](MTP_RELEASE_PLAN_2026-10-08.md) | 基础修复PR、有限支持准入与冻结验收清单 | 2026-10-09历史阶段 |
| [MTP数值准入设计](MTP_NUMERICAL_ADMISSION_DESIGN_2026-10-08.md) | 快速T4与严格普通greedy合同、状态所有权和代价 | 合同明确时 |
| [主线存储准备](STORAGE_READY_2026-10-07.md) | 可再生成缓存清理、原始证据保护与可用容量 | 本轮整理 |
| [主线收尾与分支交接](MAIN_WRAPUP_2026-10-07.md) | 精选研究工具、公共 I/O/预算修复、历史 NO_GO 索引与验证边界 | 本轮收尾 |
| [OFFLOAD_AUTONOMOUS_2026-10-03.md](OFFLOAD_AUTONOMOUS_2026-10-03.md) | 十小时自主队列、在线候选与冻结HTTP决定门槛 | 历史队列；不作为当前活动目标 |
| [OFFLOAD_REPLAY_2026-10-02.md](OFFLOAD_REPLAY_2026-10-02.md) | 实际分块/驻留离线回放与候选收益边界 | Goal完成：重排减少约6%–7% GPU补载，未接受性能 |
| [OFFLOAD_RAM_PROTOCOL_2026-10-02.md](OFFLOAD_RAM_PROTOCOL_2026-10-02.md) | RAM预算修复、缓存/实际IO协议与有界对照 | Goal完成；完整物理RAM及受限cg IO归属仍未闭合 |
| [MOE_OFFLOAD_REPAIR_2026-10-02.md](MOE_OFFLOAD_REPAIR_2026-10-02.md) | offload 基础修复 Goal 范围、身份与验证出口 | 修复与有界验证完成；总物理峰值未知 |
| [MOE_OFFLOAD_DIAGNOSIS_2026-10-02.md](MOE_OFFLOAD_DIAGNOSIS_2026-10-02.md) | offload 分支、代码缺陷、内存/验收/离线方法审查及推进建议 | 诊断快照；后续修复见修复阶段 |
| [GDN_VECTOR_LOAD_2026-09-21.md](GDN_VECTOR_LOAD_2026-09-21.md) | GDN 连续读取、完整 E2E 与状态等价证据 | 本轮验证更新 |
| [2026-09-21 状态历史](HISTORY_STATUS_2026-09-21.md) | 完整保留 fcb5925 时的阶段记录，当前状态已精简 | 历史快照 |
| [GRFrame read/write](GRFRAME_READWRITE_2026-09-21.md) | 提前 inject、frame 所有权与阶段验收 | 验收进行中 |
| [GRFRAME_NORMED_REUSE_2026-09-21.md](GRFRAME_NORMED_REUSE_2026-09-21.md) | normed 区域复用候选、容量边界与 E2E 门禁 |
| [DECODER_WORKSPACE_LAYOUT_2026-09-21.md](DECODER_WORKSPACE_LAYOUT_2026-09-21.md) | decoder 容量与偏移单源化候选及布局对照 |
| [NORMED_MOE_ALIAS_2026-09-21.md](NORMED_MOE_ALIAS_2026-09-21.md) | normed 借用 MoE 区域的别名、生命周期与容量验收 |
| [GRREAD_SCRATCH_2026-09-21.md](GRREAD_SCRATCH_2026-09-21.md) | GRRead down/up 接受，分配各 554→362/步，性能持平 |
| [GRFRAME_GATE_2026-09-21.md](GRFRAME_GATE_2026-09-21.md) | gate 工作区已接受，分配各 362→266/步，新增 64 KiB |
| [DECODER_RESOURCE_CONTRACT_2026-09-21.md](DECODER_RESOURCE_CONTRACT_2026-09-21.md) | 资源合同已接受，布局与错误合同检查通过，性能持平 |
| [PREFILL_CHUNK_SEQUENCE_2026-09-21.md](PREFILL_CHUNK_SEQUENCE_2026-09-21.md) | 文本分块 prefill 序列接口候选 | 状态边界演进时 |
| [SERVE_READBACK_COMMIT_2026-09-21.md](SERVE_READBACK_COMMIT_2026-09-21.md) | serve GPU 结果发布边界候选与验收范围 | 跟踪结果提交时 |
| [LINEAR_SCRATCH_OWNER_2026-09-21.md](LINEAR_SCRATCH_OWNER_2026-09-21.md) | 线性 scratch 所有权复核通过，时间线运行中 |
| [GRREAD_PAIR_MIX_2026-09-21.md](GRREAD_PAIR_MIX_2026-09-21.md) | GRRead 成对读取已接受，TTFT 改善、decode 持平 |
| [GRWRITE_READ_FUSION_2026-09-21.md](GRWRITE_READ_FUSION_2026-09-21.md) | 首版未接受；向量读取第二版完整验收通过，TTFT 改善 |
| [短路径 top-k](SHORT_TOPK_2026-09-21.md) | 精确选择、完整 E2E 与局部时间线 | 本轮验收完成 |
| [短上下文索引打分](INDEXER_DECODE_2026-09-21.md) | CTA 映射、逐位分数与配对 HTTP | 本轮验收完成 |
| [Decode 多级 top-k](DECODE_TOPK_2026-09-21.md) | 寄存器网络、完整 E2E 与逐位证据 | 本轮验收完成 |
| [QSA 输出列分片](QSA_DECODE_SPLIT_2026-09-21.md) | 单 token 四分输出、完整 E2E 与逐位证据 | 本轮验收完成 |
| [MoE 设备执行](MOE_DEVICE_DECODE_2026-09-21.md) | 单 token GPU 专家执行、五档 E2E 与同步计数 | 本轮验收完成 |
| [TOPK_REGISTER_NETWORK_2026-09-21.md](TOPK_REGISTER_NETWORK_2026-09-21.md) | 流式 top-k 网络寄存器化候选与验收 | 本轮验证更新 |
| [HC_GATE_FUSION_2026-09-21.md](HC_GATE_FUSION_2026-09-21.md) | HC 投影与 gate 融合候选和验收 | 本轮验证更新 |
| [GEMV_STAGING_2026-09-21.md](GEMV_STAGING_2026-09-21.md) | BF16 GEMV 输入搬运候选与验收 | 本轮验证更新 |
| [HC_INJECT_GEMV_2026-09-21.md](HC_INJECT_GEMV_2026-09-21.md) | HC 四输出 decode 投影形状分析与验收 | 本轮验证更新 |
| [LINEAR_WORKSPACE_2026-09-21.md](LINEAR_WORKSPACE_2026-09-21.md) | 线性注意力临时缓冲生命周期及合并分配验收 | 本轮验证更新 |
| [MOE_ROUTING_ALLOC_2026-09-21.md](MOE_ROUTING_ALLOC_2026-09-21.md) | MoE 分配隔离实验与合并分配验收 | 本轮验证更新 |
| [MOE_WORKSPACE_2026-09-21.md](MOE_WORKSPACE_2026-09-21.md) | MoE 路由元数据生命周期与 E2E 验收 | 本轮验证更新 |
| [SPARSE_LAYOUT_2026-09-21.md](SPARSE_LAYOUT_2026-09-21.md) | 稀疏注意力 shared 行布局、E2E 与逐位数值验证 | 本轮验证更新 |
| [TOPK_WARP_2026-09-20.md](TOPK_WARP_2026-09-20.md) | 流式 top-k warp 交换、失败候选与 E2E 验收 | 本轮验证更新 |
| [OUTPUT_LIMIT_2026-09-20.md](OUTPUT_LIMIT_2026-09-20.md) | 输出上限后的无用前向修复、边界与五档 E2E | 本轮验证更新 |
| [HTTP_TIMELINE_2026-09-20.md](HTTP_TIMELINE_2026-09-20.md) | 已验收版本的五档 HTTP 时间线、阶段边界与优化优先级 | 新诊断更新 |
| [POSITION_METADATA_2026-09-20.md](POSITION_METADATA_2026-09-20.md) | 主模型位置回读消除与 E2E 验收 | 本轮验证更新 |
| [INDEXER_CLEANUP_2026-09-20.md](INDEXER_CLEANUP_2026-09-20.md) | 索引键无用计算清理与等价性/性能验收 | 本轮验证更新 |
| [ACCURACY_AUDIT_2026-09-20.md](ACCURACY_AUDIT_2026-09-20.md) | QSA 数学错误定位、分步质量 E2E 与性能验收 | 本轮验证更新 |
| [BASELINE_2026-09-20.md](BASELINE_2026-09-20.md) | 五档普通 decode 初始 E2E 结果、证据与限制 | 新测量单独成文，不覆盖历史结果 |
| [REPRODUCIBILITY_2026-09-20.md](REPRODUCIBILITY_2026-09-20.md) | 稀疏注意力输出非确定性定位与 E2E 证据 | 后续验证更新 |
| [DISCONNECT_FIX_2026-09-20.md](DISCONNECT_FIX_2026-09-20.md) | SIGPIPE 修复、断连 E2E 与输出复现阻塞 | 新验证追加记录 |
| [EVALUATION.md](EVALUATION.md) | E2E 优先规则、五档矩阵、证据与接受条件 | 用户规则或评估口径变化时 |
| [STATUS.md](STATUS.md) | 当前状态快照: 当前焦点、进行中、卡在哪、下一步 | 每次有意义的改动后 |
| [DONE.md](DONE.md) | 已完成 / 已解决归档 (从 STATUS 分离) | 条目完成时追加 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 整体架构设计: 模块划分、数据流、关键设计决策 | 架构变化时 |
| [PHASES.md](PHASES.md) | 分阶段计划: 每阶段范围、完成标准 | 阶段推进时 |
| [log/](log/README.md) | 开发日志 (按日期分文件, 时间倒序, 不可变) | 每次有意义的改动后 (追加到当天文件, 不改历史) |
| [MODEL.md](MODEL.md) | 目标模型架构笔记: qwen4_exp 各组件的理解 | 理解深入时 |
| [REFERENCE.md](REFERENCE.md) | 参考项目说明: 每个项目取什么、怎么用 | 参考策略变化时 |
| [REFERENCE_MTP.md](REFERENCE_MTP.md) | MTP / 投机解码参考调研 | MTP 相关时 |
| [MTP_BATCHING.md](MTP_BATCHING.md) | MTP 批处理设计与阶段 | MTP 批处理推进时 |
| [DATAFLOW_OPTIMIZATION.md](DATAFLOW_OPTIMIZATION.md) | 当前数据流预算、假设与决策边界 | 数据流优化推进时 |
| [MOE_DISTRIBUTION_2026-09-28.md](MOE_DISTRIBUTION_2026-09-28.md) | 历史轨迹专家ID分布、阶段/层/场景差异和稳定性 | 统计完成，与缓存命中分开 |
| [MOE_SHADOW_2026-09-28.md](MOE_SHADOW_2026-09-28.md) | 有界请求完成边界影子观察、完整长度验证与资源成本 | 功能通过，严格持平未通过 |
| [MOE_CACHE_REPLAY_2026-09-28.md](MOE_CACHE_REPLAY_2026-09-28.md) | 固定容量静态/LRU回放、预算与影子阶段决策 | 本轮离线交付 |
| [MOE_LAYER_TOPN_2026-09-28.md](MOE_LAYER_TOPN_2026-09-28.md) | 每层Top-N完整曲线与覆盖/整组目标的最小名单 | 同样本描述性，非运行时最优 |
| [MOE_HYBRID_2026-09-28.md](MOE_HYBRID_2026-09-28.md) | 固定热点与动态LRU混合容量离线比较 | 离线探索，不认定统一最优比例 |
| [MOE_EVALSCOPE_SCENARIOS_2026-09-28.md](MOE_EVALSCOPE_SCENARIOS_2026-09-28.md) | 24条多场景采样、32/64槽影子统计与128/256离线容量 | 采样完成，非生产代表性 |
| [MOE_ROUTING_TRACE.md](MOE_ROUTING_TRACE.md) | MoE受控采集边界、首轮证据与下一阶段离线缓存回放 | 设计或实现进展变化时 |
| [MOE_RESIDENCY_PLAN_2026-09-30.md](MOE_RESIDENCY_PLAN_2026-09-30.md) | 分层专家驻留与按需加载实现计划、冻结轮与用户决策记录 | 历史计划与冻结轮记录 |
| [MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md](MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md) | 分层驻留验收报告（第一轮矩阵实测已填） | 历史第一轮报告；非当前待跑清单 |
| [MOE_RESIDENCY_L2_PREDICTION_2026-10-01.md](MOE_RESIDENCY_L2_PREDICTION_2026-10-01.md) | L2 命中率离线预测与 C 容量扫描（第二轮预判/第三轮决策输入） | 已完成（离线分析） |
| [MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md](MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md) | 262144容量完整内存账（预算侧+实测侧） | 历史内存账与当时缺项 |

## 现行入口（2026-10-17）

先读 [STATUS.md](STATUS.md) 与 [EVALUATION.md](EVALUATION.md)。
常规改动的测量、统计与工程决策分工见
[性能决策协议](PERFORMANCE_DECISION_PROTOCOL.md)。
当前主线工具与规则交付见[收敛记录](MAIN_CONVERGENCE_2026-10-17.md)，
默认MTP与PLE修复验收见[数值收口](MTP_NUMERICAL_CLOSURE_2026-10-16.md)。
[Offload 分支收尾](MAIN_WRAPUP_2026-10-07.md)是此前阶段历史。
运行时部署与旧基线保留[首版私有文本版本](RELEASE_TEXT_V1_2026-09-28.md)
及[基线交接](BASELINE_HANDOFF_2026-09-28.md)的历史记录；源码合并不等于
覆盖原开发目录的默认二进制。
[收敛清单](CONVERGENCE_2026-09-28.md)已完成；上表专题中的
“候选”“进行中”是当时记录，不能作为当前待办或发布结论。

性能优化第一项测试仍为evalscope HTTP E2E；Bug修复可先做直接
回归。不使用bench。先冻结范围、成组修改、统一验收，只有具体
失败才做定位/消融；未改动且身份一致的证据复用。

[历史状态快照](HISTORY_STATUS_2026-09-20.md) 与
[历史数据流分析](HISTORY_DATAFLOW_2026-09-20.md) 保存旧内容，不作为现行结论。
[历史 AGENTS 状态段](HISTORY_AGENT_ENTRY_2026-09-20.md) 也已归档，入口不重复维护状态。
日志原文保持不变，后续更正以新条目说明。

## 写作原则

- **记录事实与决策, 不写空话**。每条记录应能让未参与的 agent 据此
  继续工作。
- **决策要写理由**。"做了什么" + "为什么" + "排除了什么替代方案"。
- **代码是最高事实来源**。文档描述的是"设计意图", 实现细节以代码为准。
- **日志只追加**。开发日志按日期分文件于 [log/](log/README.md); 历史条目
  不修改, 发现错误时追加更正条目; 新条目追加到当天 `log/<日期>.md` 顶部
  并在 log 索引登记。
- **STATUS.md 是快照**。始终反映"现在"; 已完成条目移入 [DONE.md](DONE.md),
  历史时间线由 [log/](log/README.md) 承载。

- [固定 k3 suffix 同步收尾合同](MTP_SUFFIX_SYNCHRONOUS_2026-10-26.md)

- [固定 k3 suffix 报告路径绑定修复](MTP_SUFFIX_REPORT_BINDING_2026-10-26.md)

