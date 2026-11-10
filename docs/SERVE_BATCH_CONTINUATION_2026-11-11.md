# Batch后台子任务的真实终态闭合

2026-11-11进展：步骤1—3已实现并经[26不同CPU检查](evidence/serve-batch-continuation-cpu-20261111/README.md)接受，包含真实回执丢失、进程死亡、排空、取消/期限/新代撤权和TTL收取/释放。辅助观察器不可用且提交记录不存在的有限结束也已补齐。步骤4失败/取消实际计量继续实现，步骤5业务/兼容回退与全部总任务继续；0GPU/默认保持。以下问题描述为起始事实，不能把已完成CPU组件当作全部交付。

本项承接A09.6，不新增范围豁免。公开入口与原结果usage已分别在
`d099b00c`、`2b71668e`通过针对性CPU/SDK；真实模型与发行仍未接受。
当前问题：background Responses把原预留和data许可转交给实际任务所有者，
但Batch行目前把queued回执当完整结果，随后可以提前汇总和结束。必须修复。

## 实现顺序与不变量

1. **先持久绑定，后允许提交。** 由可信Batch所有者预留ResponseID，
   和原行Start票据同事务保存。通过私有C++上下文传到原生Responses，
   以同一ID创建原记录；输入JSON/metadata不能提供或覆盖关联。
   Response私有计划保留BatchID、行号、原request_id和scope绑定，
   Begin/恢复均核对。任一开始或绑定回执不明不得重发模型。
2. **让出等待，读取同一终态。** 行在queued/in_progress期间继续running，
   Batch公平轮转其他任务；不在Batch侧重复持有已转交的data/分钟预留。
   必须先读取绑定的原Response状态，只有原终态及执行排空可确认后
   才完成Batch行/汇总，保存原工具输出、usage和错误。PID身份不明、
   暂时读失败保留等待；明确不存在且旧所有者已消失才按中断结算。
   原24小时/行deadline不延长，已有started票据不重放。
3. **取消和删除有确定边界。** Batch取消/到期/撤销客户端沿原绑定发送给
   既有Response所有者，并等待实际排空；关服只停止自己的工作。
   Batch保存结果之前，终态Response不能被显式删除或TTL回收破坏：
   私有引用保持结果，删除若仍被活动行使用返回临时冲突，结果已接收后
   自动解除。核对跨客户端、Response终态与Batch复制之间的竞态。
   不以无限pin替代释放，不删除状态来制造恢复成功。
4. **采集可获得的失败用量。** 从实际请求/模型步骤的计数采集已完成输入、
   输出及cache/reasoning细分，不把预算当使用量。正常错误/取消排空后的
   数据应随行结果原子保存，供原结果汇总与重启补计。进程真正死亡且
   未保存的尾部仍标为未知，不能伪造零或声称完整；需要区分可采集而
   尚未接线的缺口和不可重建的历史事实。
5. **针对性验收后连续交付。** 成组开发后冻结CPU清单，覆盖绑定前后真丢回执、
   queued/live/terminal、真实子进程死亡、取消/到期、同一ID不重放、
   准入让出、终态删除竞态及实际用量；复用未受影响证据。
   之后继续永久输出容量失败的有限终结、辅助所有者初始构建失败的
   有界恢复，再冻结适用模型业务与兼容状态/配额回退、默认发行。

## 已确认的接线位置

- `BatchState::StartRow/FinishRow`与`BatchJobs::RunRow/Turn`：开始票据、
  原结果及公平调度。当前running恢复只看原进程/本地drain，需要先识别
  持久关联，不能把已转交的后台执行误记为丢失后再次提交。
- `ChatServer::RunBatchRequest`和`HandleChat`：已通过共同有限响应上下文
  及冻结输入调用原生端点；预留ResponseID/可信父关联从这里传入。
- `ResponsesExecution::RestoreInputs/Begin`、`ResponseStatePlan`与
  `ResponsesState::Begin/LoadPlan`：准备与持久私有计划；Begin回执未知
  必须复用现有不重放/对账协议。
- `ServePreparedBackgroundResponse`、`ResponsesJobs::Adopt/Execute/Cancel`：
  原data/Batch分钟预留已转交且原期限保存；不能新增第二份RPM预留。
  需要给原Batch观察者明确结果可接收/执行排空的依据。
- `ResponsesState::Erase`当前仅检查终态，然后直接擦除；须补父行引用
  的有界保护。`object_retention.cpp`需认识新引用、原归属和释放条件。

该计划全部为必做项。子组件CPU接受、unknown披露或queued回执均不是
A09/总目标完成；不引入外部前置服务，不自动重启模型。用户负责长稳实跑，
本任务继续全部实现、针对性短验、可回退交付与其余总计划。
