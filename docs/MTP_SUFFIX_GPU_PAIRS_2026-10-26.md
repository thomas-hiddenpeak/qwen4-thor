# 子集200K/8K已记录GPU执行对照：有界停止

入口cf14f7b。真实8K参数账本已通过，但不能跨进程反填旧或新Nsight
缺项。本阶段只补原四臂GPU执行记录对照，不改推理程序、suffix准入或
草稿head，不部署，不重跑已通过且身份不变的重复收益/正确性/质量。

scope SHA `e1b10eea282aa942b9adcfc423f3f2b4b3e31cc5a1ec952cc419caf0de1439db`。
工作7200秒，0build，CPU≤40方法/合计120秒，最多2suite且第二次仅修
具体失败；唯一GPU lease4200秒（预留600秒清理），4服务×≤900秒，
6 HTTP、4 Nsight capture；0新fixture/重试/额外warmup。每capture导出和
分析各≤120秒，日志≤256MiB/SQLite≤8GiB。终审封存≤180秒。

固定顺序subset 8K OFF→ON→200K ON→OFF；200K两服务都先同一8170/256
前驱，再捕获204781/256。同f0ff、显式65536行BF16、single greedy k3，
max_prefill8192/max_len208896；自然继承页缓存、不调功耗/亲和性/缓存。
固定Nsight2026.5.1同步repeat:1:sync，cuda,nvtx；不混用CUPTI预加载。
完整driver从加载、HTTP、收尾到导出分析持同一共享独占锁。

新适配器只修具体工具缺陷：launch CLI正常rc0不能替代实际目标存活；
request-end须限[q4t][request]，避免误读capture-end；每capture独立
NVLOG快照、终态追加审计和实际目标报告/退出绑定。旧strict结果完整
保留，不把通用warning删除或升级为全trace PASS。

限定比较门：API/CPU/NVTX身份完整、已记录kernel与成功launch双向唯一
关联、原输出和工作量一致、无明确loss/overflow/新警告/缺kernel/copy。
仅已知两类目标通用warning可在该次终止标记和正常退出证据下保留为
“与已知过滤机制相容、整体完整性未证”。缺活动cudaMemsetAsync继续
PARAMETERS_UNOBSERVED_IN_THIS_CAPTURE，不按另进程实参或数量填零。

分别比较target prefill、prompt draft extend、首生成轮和后续轮的
已记录GPU时长、并集、签名与数量，以及对应host/API区间。wall−threadCPU
只称非运行墙钟，sync host时长不称GPU busy/wait；父子区间不重复加总，
内部活动空隙不称GPU idle。单配对仅描述性诊断，不替代正常性能验收。

未知错误、关联/输出合同失败立即停止；竞争、监控或强制/失败清理记
ENV_ABORT，只清理自有进程。完整对照仍不能归因则记录归因不足，不自动
补200K参数账本、扩大样本或扫描设置。原五档测量已齐但保护NO_GO保持；
本阶段结果分别记录证据有效性、测量完整性、统计结论及工程决定。


## 结果：ENV_ABORT，未形成GPU配对

CPU 37方法首suite全部通过，1.326263秒；428个冻结源无变化。
实际plan `ec39fba4e8300241fe42c06bcc55dc65725ab42d2253889ee679b6e2fb6cc7c6`，
23211文件身份、1765包条目及197模型元数据在终审核对一致。无构建、无
推理代码修改。实际观测程序仍f0ff；默认f356/sidecar422993b0/子集b63b0f25
保持，不能把本次工具提交当作新的运行时构建身份。

唯一阶段80.064455秒，实际启动1模型、完成1个evalscope HTTP：8170输入、
256输出、finish=length，输出SHA仍66bb6d8c…436e。host记录独立核对
74次四行verify、148次神经proposal、74次accepted extend共252行、
22次restore、接受分布[7,8,7,52]，77个scheduler waits减74推导3次尾部T1。
这些只是已保存host工作量，未导出GPU trace，不能写成GPU执行已证明。
插桩HTTP TTFT5.994203秒、latency12.720991秒、decode37.908136 tok/s，
仅失败诊断原值，不能加入正常吞吐/TTFT接受样本。

首组收尾时NVLOG达746229179字节（约711.66MiB），超过事前256MiB上限。
容量检查抛错；同一检查又在shutdown控制进程spawn前抛错，实际
shutdown receipt的pid/returncode均null。目标q4t、wrapper/launcher均
正常rc0，无强制目标kill；但同步report-ready和shutdown合同未完成，
故不能据进程正常退出改判采集成功。22,260,511字节trace.1.nsys-rep保留
为未绑定原件，0导出、0strict审计、0有效capture，后3组均未启动。
驱动state原样STOPPED、completed=[]、requests_completed=0；此计数表示
通过整个臂的请求数，与实际已返回1个HTTP响应分别记录。postflight为
ENV_ABORT，外层lease保留COMMAND_FAILED/rc1，不改写历史状态。

## 具体失败链路和观测限制

一次失败原件流式统计17.508659秒，完整读取2066081行/746229179字节，
限1GiB/20秒，属于最终180秒终审内的取证，不是重新接受超限capture。
两个logger合计743527473字节，占99.64%：

| logger | 字节 | 行数 |
|---|---:|---:|
| quadd_verbose_daemon_evtsrc_trace_events | 253262865 | 1025775 |
| quadd_verbose_daemon_evtsrc_trace_event_details | 490264608 | 1025602 |

当前global100iI+ForceFlush把逐活动序列化和详情全部写出；ForceFlush是
日志文件刷新，不是CUPTI activity flush。关闭无关logger并不足以使现有
必要logger低于256MiB；不能事后加大上限、去掉完整性门或仅禁verbose
后继续声称保留了本次consumer终止标记证据。

目标进程673条header中保存11条E：5条与已观察HES失败/软件fallback类型
相同；另有activity kind36/35/34/33不能在context级disable的4条，以及
2条MemoryStorage析构错误（AllocatedCount/ReturnedCount各自相等：
35/35、1020242/1020242）。这6条未获原有限分类门认可；相等计数不能
证明无事件丢失。即使修复容量/清理耦合，也不能自动取得采集资格。
统计中保留了producer/consumer终止标记及late-filter例子，但未做该次
源PID批次/stop/strict关联审计，不将其提升为warning误报或完整性证明。

明确可修点是测量失败检查与清理检查耦合：锁存超限错误后，正常有界
自有清理应仍能执行；原错误、ENV_ABORT和后续停止必须保持。日志容量
方案和6条新增错误仍需先有具体机制解释；本阶段没有新的运行时原因，
未改suffix准入、prefill或神经草稿，不追加GPU采样来换取通过。

## 终审、资源与工程决定

81/81资源采样全部落盘，pending0、无监控失败；失败路径自有step-PGID
清理后barrier完成，这不是对全部detached后代的正常成功barrier。最终
88个已记录PID均ABSENT，3个自有PGID无残留，共享锁无持有记录。原件在
终审/归档期间未变，driver原seal无差异。系统统一RAM代理峰113831604224
字节、MemAvailable最低18089033728字节、swap使用峰2047721472字节；
它们含其他进程及页缓存，不能相加CUDA host/device/managed分配，也不是
collector净增量。三项PSI不可读保持UNAVAILABLE，没有补零。

| 字段 | 结论 |
|---|---|
| 证据保存完整性 | PASS；冻结身份、失败原件、清理及归档可核验 |
| GPU对照证据有效性 | ENV_ABORT；不授予有限计时或整trace资格 |
| 测量完整性 | 1/6实际HTTP、0/4有效capture、无完整OFF/ON pair |
| 统计结论 | NOT_EVALUATED_INCOMPLETE_PAIRS；无200K/8K GPU归因 |
| 工程决定 | PERFORMANCE_NO_GO_UNCHANGED；保留当前默认，不部署suffix |

两个终审脚本合计24.505138秒，无强制清理/源变动；936文件803964315字节
独立复制、SHA/inode回读通过，含本次完整NVLOG及报告。归档非自包含，
运行库/模型/工具包等仍有明确原路径SHA外锚，不称交付回退包。

- terminal review SHA：eefc95476a493c1a977cbd9f4386dcd29773ef39251faf6a695579a9beb391f8
- raw seal SHA：876e44a7e57fb0ced443364c55757ef730d2eb7ce9cc73606a6b04c68478290e
- archive manifest SHA：2546770111f88a6a345f3b5664dc6c8989d8440435375ae23bc4d65e0aa5019a
- archive seal SHA：7ab42efc648acf0545514b4ecb7b6adc26b76d85de3d9d8c9332ce8472b3613d

原始工作区`.q4t-work/suffix-gpu-pairs-20261026/`；
[紧凑证据索引](evidence/mtp-suffix-gpu-pairs-20261026/index.json)绑定终审及
最终wrapper收据。已通过且依赖未变的两head重复收益、直接合同及质量
证据明确复用；原250请求五档已经测齐但保护未通过。本次工具阶段不
改变该决定，也不完成整个交付目标；200K/8K GPU对照、具体运行时归因
及最终接受条件仍未闭合，完整目标保持active。
