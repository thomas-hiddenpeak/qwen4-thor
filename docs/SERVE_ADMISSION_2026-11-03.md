# SRV-02b 请求准入与容量适配排队开发合同

2026-11-03排入下一独立工作包。类型为业务可靠性；当前尚未实现，
不作调度性能改善声明。沿用当前工作分支，不启动额外后台服务。
SRV-03b严格长payload没有进入写阶段，其失败记录继续保留，不能用
本任务通过代替背压证明，也不复用/重置03b的生成或启动预算。

## 已确认代码问题

- chat/preflight共用`TextAdmissionLimit(max_seq)=max(32,2*max_seq)`，
  不能按使用人数配置；`active_chats_`包括预处理、等候和执行，
  不是纯GPU在跑数量，也不是已排队数量。
- `AllocSeqId`每100ms轮询并在`notify_all`后重新竞争；
  容量best-fit存在，但先来的兼容请求没有优先保证。
- `/v1/config`只报告固定推导值。`slot_wait_seconds`已有，
  没有当前slot等候数量、等候拒绝及入口准入拒绝的独立计数。
- body预留48MiB、连接上限及strict单租约分别存在；这些边界不能被
  新的请求数配置取代。预处理还没申请slot时不计入slot队列。

依据：`chat_generation.cpp`、`chat_preflight.cpp`、`chat_scheduler.cpp`、
`chat_plan.cpp`、`chat_metrics.cpp`、`server_options.h/.cpp`。

## 拟实现的对外合同

1. 新增`--max-inflight-requests N`：chat和preflight共用的请求所有权
   上限，覆盖当前准入位置之后的预处理、排队、执行和输出。省略保持
   原`max(32,2*effective_max_seq)`，显式范围1..4096；它不是连接、
   HTTP body或物理内存预留，也不保证达到配置值的实际并发。
2. 新增`--max-queued-requests N`：仅slot等候上限，显式0表示没有等候
   容量；省略取`max(0,effective_inflight-effective_max_seq)`。
   显式范围0..4096，不能大于最终inflight上限；有效值必须在模型预算
   确定effective_max_seq后计算，不根据用户原始请求的槽数猜测。
3. 队列次序在输入/分词/grammar准备后、进入`AllocSeqId`时确定，不称
   HTTP到达FIFO。每次选最早能使用当前空闲槽的请求，再做容量best-fit。
   早到的大请求暂时不能使用小空闲槽时，小请求可继续；出现足够大的
   空闲槽时，后来的兼容请求不得抢先。不是每租户公平或完成时间保证。
4. 立即可服务且不抢先的请求不占等候名额；队列满时也可使用不影响
   更早请求的兼容空闲槽。确需等候而达到上限，返回503和稳定的
   `queue_capacity_exhausted`；已提交SSE头的请求使用已有SSE错误终态
   携带相同code，不能再发第二HTTP响应或成功尾帧。HTTP状态和逻辑
   拒绝分别记录。总准入拒绝与它分开，不能假称GPU故障。
   没有自动客户端重试，不增加未经确认的幂等承诺。
5. 等候中的取消、断连、deadline、shutdown、GPU失败和异常出口都要
   注销同一等待项，唤醒后继；已交给请求的槽只由其唯一所有者释放。
   原100ms断连/截止轮询边界保持，显式取消/释放主动通知。清理不执行
   新GPU工作，不借移交队列改变scheduler的数值或KV退休合同。
6. `/v1/config`公开请求值/有效值及策略名称，CLI帮助与机器合同同步。
   metrics新增固定名称的`q4t_queued_requests`、
   `q4t_queue_rejected_total`和`q4t_text_admission_rejected_total`；
   复用已有slot等待直方图，不增加请求ID等无限基数标签。

以上数值边界为配置解析的工程范围，不是吞吐或RAM承诺。连接、body、
strict和模型容量仍分别执法；不能用提高inflight绕过任何已有门。

## 开发及接受顺序

先实现可独立验证的容量队列/配置推导，成组接入chat、preflight、
有效配置、CLI与metrics，再做针对性CPU合同。至少覆盖零队列、满队列、
FIFO不抢先、大小槽绕过阻塞、取消首/中/尾项、异常注销、停止/失败，
并验证预处理未进队列及配置在预算收缩后正确推导。

对新增协议/实现及生命周期完成审查后，另行冻结具体真实HTTP清单、
生成/拒绝/启动/总时长上限，再启动独占GPU验收。实际队列、取消和恢复
必须观察，不能由CPU helper通过推导。真实工具回填及默认MTP质量保持
为业务出口；必要时双槽plain用于异容量合同，双槽MTP不自动扩大范围。
已有同身份的不受影响证据明确复用；不用03b失败夹具反复启动。

本批完成后才推进body拒绝与写超时/错误细分、入口访问及长稳。
03b仍待独立解决载荷生成与取证前提；当前未冻结额外模型重试计划，
不自动扩样或重置原预算。
