# SRV-03b 严格输出背压与受载控制回补

当前结果：[本轮未通过及取证边界](SERVE_BACKPRESSURE_RESULT_2026-11-03.md)。
1生成/1启动，长strict未在输出预算内闭合、没有EAGAIN；原预算不扩大，
默认不变、GPU释放。以下保留冻结范围及当时准备情况，不表示真实验收通过。
下一独立包见[02b准入/容量FIFO开发合同](SERVE_ADMISSION_2026-11-03.md)。

2026-11-03冻结，基线5a460981，默认5642eb68/6aca6a3e。属于业务可靠性
验证，不是性能优化。先闭合A组未观察到的严格长payload实际写阻塞，再
单独推进队列公平、容量配置和资源拒绝观测，不把多个目标混为一次验收。

## 现有事实与遗漏

| 边界 | 已有代码 / 证据 | 仍需完成 |
|---|---|---|
| 文本请求 | TextAdmissionLimit=max(32,2*slots)，chat/preflight共用；与GPU槽独立 | 尚无可配置等候上限或FIFO公平保证 |
| 连接 | 业务max(128,8*slots)，额外4控制；健康/取消在分类后预留 | 未分类慢header可占满总上限，未承诺抵御洪泛；metrics仍属业务入口 |
| 输入字节 | 单chat/preflight16MiB，合计48MiB，header64KiB，绝对读截止30秒 | body预算是序列化输入预留，不是解析副本或整机RAM硬限；合计字节拒绝尚无专用错误原因 |
| strict | 单独1租约，持有到响应/worker清理；词表32MiB、grammar storage64MiB、worker2GiB地址空间 | 地址空间不等于整机RAM/RSS限额；多客户端受载需要实际观测 |
| 响应写 | MSG_DONTWAIT，EAGAIN后最多25ms轮询，单次写最多30秒并受请求deadline限制 | 普通流实际EAGAIN已有A组证明；严格整份缓冲响应未闭合 |
| 等候与失败 | slot等候每100ms检查取消/断连/deadline；notify_all重新竞争 | 没有按到达顺序排队；写失败/写超时仍合并记为client_disconnected |

依据：`http_request.h/.cpp`、`chat_http.cpp`、`chat_scheduler.cpp`、
`chat_generation.cpp`、`http_response.cpp`、`server_options.h`、
`structured_admission.h`。上表后续项是代码确认的边界，不以已存在上限
推导公平性、抗洪泛或整个服务的RAM硬保证。

## 本批执行范围

1. 固定一个大于12KiB的有限JSON schema输出：value为48项数组，每项
   固定256个等号，合计12288个。保持enum单值256字节、数组64项和
   schema16KiB上限。先读取真实tokenizer做CPU预算估计、
   用生产请求探针检查；编码长度仅作估计，不保证模型实际分段或成功。
2. 同源专用构建启用已有Q4T_SERVE_FAULT_INJECTION，只将实际TCP发送
   缓冲调小，并在真实EAGAIN点握手。客户端在connect前设置小接收窗口，
   不读取响应；不注入伪模型内容、不用sleep或慢生成代替写阻塞证明。
   专用构建不安装，不修改模型/数值路径，协议/源码身份与默认核对。
3. SSE长严格输出到EAGAIN后：提交一个普通请求等候，观察独立strict
   503拒绝，验证health/ready和错误取消凭据；正确取消接受后释放握手，
   验证没有成功尾帧、资源回收、普通请求回答710003，再验证短严格JSON。
4. JSON长严格输出到EAGAIN后：同样提交普通等候与strict过载探针；
   释放握手但保持客户端不读，观察既有30秒写截止退出。请求deadline
   固定180秒，必须确认写阻塞先于请求deadline；原始JSON应不完整，
   不能当成功响应。核对普通请求恢复和短严格JSON、全部资源回收。
5. 记录parent/worker RSS与系统MemAvailable，明确共享物理DRAM口径。
   只形成该并发与payload范围的观察，不设未经认可的统一内存/性能阈值。

## 冻结预算与接受条件

真实阶段最多**6生成尝试、8专门负例、2模型启动、首次guard起30分钟**。
两个专用模型进程各3生成（长响应、排队普通、后续短strict），其中长
响应分别预期取消与写超时，不算业务成功；计划4专门拒绝（2 strict
过载、2错误取消凭据），其余额度只处理具体失败，不自动扩样。
既有普通JSON/SSE、真实工具和生产ELF健康按SRV-08相同身份复用，不因
文档和夹具改动再次启动默认模型。若发现运行时缺陷，先保留现场和
问题边界，按必要修复重新审查剩余预算，不能重置或扩大本批计数。

CPU先验完成后，整阶段由绝对路径gpu_exclusive持同一锁。检测竞争或
监控失败记ENV_ABORT，仅结束自有进程。首次失败保留，只重验受影响项。
两个case必须确实观察EAGAIN，并证明其发生在完整strict校验后的发送
阶段；只看耗时/缓冲占用不能过门。取消202与资源已排空分别记录；
部分响应不追加伪成功、第二HTTP响应或重复终态。

严格长payload的结论只覆盖本机真实TCP和有意缩小缓冲的专用构建。
不外推任意网络代理/拥塞/超大schema。没有运行时变更则只交付证据和
兼容矩阵，不重装相同默认ELF；阶段commit/push保留全部失败和回退链。

## 后续独立工作包

完成03b后，按优先级推进：

1. SRV-02b：明确可配置的准入/等候上限及兼容槽位的FIFO政策；保留
   取消、deadline、异容量槽避开队头阻塞。先确定业务合同，再改准入；
   不把调度策略变化称为纯文档或性能优化。
2. 资源观测：HTTP body预留拒绝、队列深度/等待、写超时/写错误的明确
   原因与计时；补受载时控制可用和统一RAM观测，避免无限标签。
3. SRV-10部署访问和持续运行：依据受控内网边界配置入口、鉴权/网关
   与限流，再冻结长稳、重启、发布/回退演练；当前不授权公开网络部署。

这些后续项尚未实现或验收；长期“完善serve到工业水准”目标保持active。

首个CPU夹具把12288个等号放进单个enum，被现有生产合同以每值256字节
上限拒绝；原探针输出与初版脚本保留，没有加载模型或扩大运行时合同。
修正为上述48×256数组，先校验已交付机器合同，再做生产探针；不把
总schema大小允许解释成单值长度允许。

修正后的CPU准备通过：schema429字节、输出12443字节，tokenizers0.23.2
固定编码243tokens；JSON/SSE两个请求均通过机器合同与生产探针。
原件在`.q4t-work/serve-backpressure-20261103/payload`和`payload-array`。
这不是grammar编译、模型真实分段、TCP背压或HTTP通过。

专用构建903c4f77已零警告完成，189运行时文件与默认相同，生成协议
文件逐字节相同；仅q4t_server的既有故障宏开启，其他Q4T配置/编译器/
Release flags核对相同。产物不安装。驱动先等真实EAGAIN，核对response
阶段日志，再提交普通请求、观察active=2/slot=0和strict拒绝；清理完毕
前客户端保持不读。原始网络前缀必须有严格内容、不能只有heartbeat，
完整客户端必须拒绝，且没有成功尾帧/第二HTTP响应。每组再检查两次
恢复、固定终态计数、worker reap和全部owner归零。

驱动CPU首次8项：7通过，1项构建门误遍历CMake导出的其他测试target，
把它们不启用故障宏误报为产品缺失；限定实际q4t_server target后该项
定向通过，原错误/脚本保留，其余7项复用。覆盖固定预算/原始截止、
guard优先、小窗口先于connect、不提前读取、部分wire拒绝、native输入、
全部owner排空和恢复路径。准备入口为`tools/serve_backpressure/stage.py`，
`prepare`封包、`launch`经共享guard完整持锁、`finalize`验证并封存；
尚未启动新GPU阶段，真实生成/背压结论待验。
