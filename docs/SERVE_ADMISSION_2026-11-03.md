# SRV-02b 请求准入与容量适配排队开发合同

2026-11-03独立工作包。类型为业务可靠性；当前源码已接入开发候选，
已完成零警告构建与CPU合同，真实HTTP尚未执行、默认程序未替换。
不作调度性能改善声明。沿用当前工作分支，不启动额外后台服务。
SRV-03b严格长payload没有进入写阶段，其失败记录继续保留，不能用
本任务通过代替背压证明，也不复用/重置03b的生成或启动预算。

## 修改前已确认的问题

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

## 对外合同

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

## 当前开发候选

已接入两个CLI值和预算后的有效推导、chat/preflight共用CAS计数租约、
无分配的容量FIFO等待项、全部返回/异常出口自动注销、503/SSE统一错误、
配置schema和固定metrics。CAS准入不先递增再回退，避免拒绝尝试暂时
混入已准入计数。`chat_admission.cpp`独立于模型计算，CPU生命周期夹具
直接链接生产AllocSeqId/FreeSeqId/RequestCancelled，验证真实线程等待、
取消/断连/截止/关闭/GPU状态/异常出口。OFF206/ON215、生产必需172、
真实线程等待6、grammar后端9均通过，生产和专用构建零警告；
运行时数值路径未修改，不把实现存在当实际负载验收。

生产候选c16caf05，专用234fae71，protocol b70fc28a。专用与生产相同
运行时源码，唯一构建配置差异为FAULT宏；只在指定请求完成生成后、
发出最终响应前暂停，最长10秒，不改logits或构造模型输出。

原生配置15项：初次10通过/5夹具失败，原因是来源应为builtin_default，
夹具误写default。相同原生probe修正5项通过，其余10项复用。
驱动11项初次10通过/1夹具失败：取消负载误用数组根；修正对象后仍因
128项超出支持64项失败，最终对象内64项通过。两次原始失败保留，
未以扩大服务schema范围迁就夹具，也未运行模型。
源码复核补齐SSE初始role前缀的验收语义，新增2项并重验受影响7项通过；
驱动现共13项，未受影响项复用。实际计划输入7种由生产native probe通过。

## 冻结真实HTTP接受计划

本包为可靠性业务任务，计划10次生成尝试、4个专门拒绝用例、2次模型
启动。硬上限12生成/8拒绝/3启动、首次获得GPU锁起1800秒（包含收尾）；
备用只用于具体失败修复，不自动重试、扩样或重新计时。所有HTTP、
两个模型组、安装健康均由同一个gpu_exclusive完整驱动持锁。

1. 生产候选，完整head/默认MTP k3，单槽8192、prefill2048、
   inflight1/queued0。合法strict SSE对象内64个true占槽后，chat和
   preflight分别返回503 capacity_exhausted；健康/就绪/存活控制仍
   可用。显式取消并确认槽、排队、body、strict/worker清理完成，
   不能只用202当回收完成。取消输出不能成为完整assistant轮次。
2. 同一生产启动随后普通JSON准确回答710003，再由固定
   Node24.18.0/openai7.47.0执行严格read_project_record真实读取
   docs/MODEL.md 1..24行，以匹配调用ID回填；最终答案包含准确
   architectures和实际引用。共4生成尝试、2专门拒绝。
3. 同源专用候选，plain、异容量8192/2048、prefill2048、
   inflight8/queued2。大请求H生成后暂停响应，保持大槽所有权；
   A、C依次进入等候队列。普通大请求拒绝503，严格大请求已提交
   SSE头后拒绝HTTP200+初始role+唯一queue_capacity_exhausted错误+[DONE]，
   初始角色声明不是生成成功；不得出现content/tool payload或成功
   finish/usage。拒绝前后模型token计数保持。
4. 队列满时小请求B使用空闲小槽并准确回答；取消头A后补入D，
   8秒内释放H的验证gate。实际slot-admission日志必须是H0/B1/C0/D0，
   A无slot授予；C、D均准确回答。最后短strict JSON恢复{"ok":true}。
   全部资源排空、worker started=reaped。此组6生成、2专门拒绝。

普通输出使用现有完整终态/usage客户端，真实工具使用固定JS SDK；
保存原请求、wire/错误体、头、指标、PPid/start-ticks内存观测、服务
日志、模型及源码身份。parent/worker RSS和系统MemAvailable分别报告，
不相加冒充唯一RAM。发生监控失败或竞争记ENV_ABORT，仅停止自有进程。

HTTP生命周期7与生产等待6同属无模型原生测试，HTTP部分仍在此锁内运行。
准入CPU的206/215/172及模型数值/模板的未变依赖证明不重复执行；
本组必须实测受影响运行时。零等候独立slot拒绝、首/中/尾取消、
断连/deadline/GPU失败和异常注销的完整排列由CPU真实生产等待验证，
不伪称每个排列都做了GPU HTTP。

通过后保留旧5642eb68/6aca6a3e及完整回退链，按已授权流程安装仅生产
candidate和新sidecar；安装路径CLI/隔离worker握手，HTTP明确复用
同SHA候选证据，不额外启动模型。任何失败保留现场、已安装时按
已知pair回退。03b严格长输出背压、真实网络部署和长期稳定仍未证明；
不作吞吐或性能非劣声明。
