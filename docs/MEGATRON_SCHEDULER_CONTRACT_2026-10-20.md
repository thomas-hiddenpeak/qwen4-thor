# 调度提交、取消与复用合同

审计基线9d46fa2，src/include与运行时b6870c0一致。本阶段未引入
新调度算法。Megatron参考固定3e2807ce中的稳定GPU视图、下一步提交
与上一结果结算的分离，只借鉴所有权方法，未移植异步调度。

## 实际状态转换

| 边界 | 生产实现与要求 |
|---|---|
| 登记和排队 | RequestRegistry拥有唯一活动ID；RequestControl取消单调。AllocSeqId只分配空闲slot，等待期间检查取消/关闭 |
| 提交 | sched_mu_保护pending/done及队列；请求对象由handler持有。普通ModelSequence::Submit快照token且不提前移动history/position |
| GPU完成 | model_mu_串行保护共享scratch；FinishHostReadback即使提交/复制失败也执行完成等待，CompleteSequenceWork按整个batch提交或失败 |
| 结果发布 | 完成后在sched_mu_下写输出、pending=false、done=true，再通知handler。停止标志不能代替完成 |
| MTP提交 | MtpSpeculativeStepMulti使用packed row→seq_id映射和每请求g；函数cleanup等待stream并在失败时无效化输出；成功后handler提交accepted_count对应history/position |
| 取消 | HTTP取消成功表示已接受取消，不表示GPU停止。已提交请求等待done才可退出；取消赢得Finish竞争后不能发送成功终态 |
| 退出 | active_移除后通知scheduler，避免幸存请求被旧lockstep计数卡住；随后ModelEndSequence和cleanup归还slot |
| 复用 | 新请求ModelBeginSequence先ResetAllLayers并等待，再Begin清空host历史；MTP另外重置自己的state。旧registry对象的Remove不能删除同ID新对象 |

## 字段归属

- token/position/history属于ModelSequence；普通提交期间只修改私有pending
  快照。PLE history由同一seq历史按该行位置构造，不能按packed row索引slot。
- seq_id是池化状态槽，不等于batch行号。KV/indexer、GDN SSM/conv及
  PLE conv按slot寻址；压缩batch必须维持token、position、history、输出
  和seq_id同一映射。现有实现重建vector，不搬移GPU状态池。
- MTP的accepted、next_b、next_d0和每请求trunk按同一request行发布；
  checkpoint恢复的是接受前缀，不能把草稿尾部作为已提交history。
- 数值上不同batch形状可能改变累加路径，因此跨形状bitwise并非通用
  调度门槛；错误slot、提前提交、取消后覆盖幸存者仍是真实合同失败。

## 本阶段直接验证和证据复用

新增server_state_五项host检查：slot顺序[3,1,0,2]→幸存[3,1,2]、
提交源复用、在途取消与完成、旧ID清理、新旧key、Cancel/Finish竞争、
截止时间和关闭的终态单调性。既有sequence_八项及scheduler_submission_
三项检查一起运行。host成功不能证明GPU KV/GDN/MTP重排正确。

当前生产未变，复用METADATA_BATCH_DEPLOYMENT_2026-10-20.md和
METADATA_FOLLOWUP_RESULT_2026-10-20.md中的普通取消8组、普通/MTP
质量各11项及多路径差分。它们不是完整MTP取消矩阵，不新增此主张。

## 后续优化的准入边界

目前提交→完成→发布串行边界是可靠性基础。增加CPU/GPU重叠前，
必须独立拥有pending输入、输出及错误状态，明确取消/失败的drain和
提交顺序；不直接删除等待。ResetState返回void的低层API仍是错误
传播局限，本次审计不把它说成已经解决，也没有设备故障恢复保证。
本任务交付是可审查合同与关键host验证，尚未承诺暂停/恢复产品功能。
