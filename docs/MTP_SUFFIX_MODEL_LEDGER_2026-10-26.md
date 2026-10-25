# 实际8K请求的memset参数机制与有界验证

本阶段局部证据有效：一次真实8170输入/256输出的subset OFF请求中，
14283次runtime memset有6381次实参count=0，7902次非零全部对应MEMSET
activity；零drops/overflow/errors。HTTP输出SHA、工作量和正常退出合同
通过。此结果补上真实模型参数证据，不是200K/8K性能归因或suffix交付。

## 原冻结范围

源入口0134c80。上一固定GEMM对照已证明两次真实count0及非零正控制，
并观察到Nsight结束标记被stop时间窗过滤；这不足以替代真实模型参数。
本阶段只诊断原8170-token、subset65536、suffix关闭、greedy MTP k3单流
请求，复用现有f0ff执行观察程序，不修改推理源码或算法。

事前scope SHA `87c8e5f92e8a08ce9ee53b0dc96fe69079973b41cea0d0d6f3a99705dc1f3cc8`。
工作3600秒；必要build≤2/合计300秒，仅具体构建失败后可第二次；
CPU≤32方法/120秒、最多两suite且仅具体失败修复。唯一GPU lease900秒：
先已有固定probe≤120秒，正常通过后一次模型加载/一次真实tools/evalscope
HTTP≤600秒，清理预留180秒。0额外warmup/重试/Nsight，环境或监控及
强制/失败清理ENV_ABORT，仅停止自有进程。预算满停止、不扩大采样。

观察器仅通过LD_PRELOAD加载到两个目标进程，父driver/evalscope客户端
保持干净环境。包装动态cudaProfilerStart/Stop，原函数各调用一次并原样
返回。记录四种runtime memset的参数/返回、driver桥与对应MEMSET活动；
SYSTEM线程编号，不假设driver与runtime correlation始终相同。仅一个
请求区间，固定flush0/1、禁用/退订，不新增CUDA同步，不cuptiFinalize。

131072条紧凑记录、8×1MiB host活动buffer、4×1MiB device池及1MiB CDP，
显式总分配≤64MiB，日志≤64MiB；不透明库RAM不能称在此硬上界内，
统一内存仍通过现有遥测记录。未知直接driver/含糊owner、非零缺活动、
未完成时间戳、错误、drops或overflow都使证据无效，不把缺项填成0。

fixture沿用同一mechanism_probe的--mode nsys（不启动Nsight、内部不订阅），
新preload为唯一订阅者；核4精确输出、0/512控制及两次生产GEMM。
模型固定原context-8192，8170输入/256输出/finish length，输出SHA
`66bb6d8c428668a991608e634c39eabebd85918b0cdd400c7b22919214ad436e`；
同时核原74×T4+3×T1工作量。自然继承页缓存，startup auto，不调亲和性、
功耗或缓存。含callback/flush诊断开销，不作为正常TTFT或吞吐样本。

参数及非零活动覆盖通过只覆盖这一次请求；不反填旧6381，不授予全trace
或模型性能资格。两head重复收益及未变正确性/质量证据按身份复用；
原五档已测齐但保护NO_GO保持。下一步仍由具体结果决定200K/8K GPU对照，
不自动扫collector或suffix准入参数。默认与发布元数据不替换。

## 实际执行与独立终审

首构建1.999855秒，`-Wall -Wextra -Werror`零warning；shared library SHA
`04e88936e20636d8a181c78c08c19d8359b4fc5636993b489665905a017cf5cc`。
CPU28首suite0.275135秒通过（analyzer18/driver10），422源码身份不变。
旧已通过mechanism及退出监控证据按未变依赖复用；不把冻结的422文件说成
422项测试。准备时核监控CPU/native/kernel-header合同及依赖SHA。

唯一GPU阶段91.088965秒，完整驱动持同一共享锁；先fixture3.619367秒，
再一次模型加载/一次真实tools/evalscope HTTP，无重试/warmup/Nsight。
fixture实际count序列0/512/0/0，512字节正控制有活动，四组精确输出通过。

| 实际模型观测 | 结果 |
|---|---:|
| runtime memset参数记录 | 14283次 |
| 实参count=0 | 6381次 |
| 非零实参／MEMSET activity | 7902／7902 |
| 非零字节总量 | 11325984640 |
| 紧凑记录 | 66555／131072 |
| drops／overflow／errors | 0／0／0 |
| 活动buffer申请／回收 | 3／3 |
| 账本文件 | 25682376字节 |

零字节调用没有对应GPU memset工作并不等于漏采。本次6381的数量与旧trace
疑问一致，支持已知机制解释；不同进程的correlation和实参仍不可互填，
旧trace不能因此被逐条认证。仅收四种runtime memset与MEMSET活动，没有
采kernel/copy时间，也未覆盖ProfilerStart之前的启动分配。

原输出SHA完全一致、finish=length、8170/256 tokens；74个闭合的
`mtp.verify_forward` T4 host spans，接受直方图7/8/7/52，77次调度等待。
尾部3次单行工作由77−74核对，不是直接观测到三个GPU forward。
共77批/2716 spans/77 flushes完整。带观察器HTTP TTFT5.753297秒、
latency12.400841秒只留诊断记录，不纳入正常性能对照或收益结论。

显式collector owned38934792字节＋配置CUPTI池5242880字节，低于64MiB；
不透明CUDA/CUPTI分配不在此硬边界内。92/92统一RAM采样落盘，系统
`MemTotal−MemAvailable`峰值112724590592字节、最低available19196047360
字节；该系统代理量包含其他进程，不能当collector增量RAM，更不能把
host/device/managed重复相加。PSI三项原本不可用如实保留。单次sample最长
0.051059秒；一次PF_EXITING pending正常确认，最终active0，无零填充。

实际fixture/q4t退出均0且无强制清理；独占guard86次观测/86次NVML查询
无竞争或监控错误。最终fresh sample在关闭屏障之后；终态94个已记录PID
均不再活动，三个自有PGID无残留，共享锁释放。轮询不排除短于间隔的干扰。

独立终审共3.467663秒，核21284文件与197模型metadata、原始封存和输出
SHA，PASS且无错误，不重跑分析器或推理。增量独立归档1206文件、
119350446字节，20846外部SHA依赖仍需保留，明确非自包含。
封存时终审进程尚在运行的执行日志按当时内容复制；最终日志及wrapper
退出单独由stage-close绑定，不修改原archive manifest。

## 四项决定与剩余工作

| 维度 | 决定 |
|---|---|
| 证据有效性 | VALID_ACTUAL_8K_MEMSET_LOCAL_ONLY |
| 测量完整性 | 本次固定fixture＋实际8K参数合同完整；200K/8K OFF/ON GPU对照未完成 |
| 统计结论 | NOT_EVALUATED_DIAGNOSTIC_ONLY |
| 工程决定 | 实际零字节机制成立；无运行时性能修复、无性能验收、无部署 |

保留原`MEASURED_REQUIRES_REVIEW`驱动状态，独立终审给出上述限定结论。
`kind=timing`是旧分派标签，内部`nsys_profile`只复用主机capture标记解析，
都不能表述为本轮运行过Nsight。原五档已测齐，保护NO_GO仍未解除；已通过
两head重复收益、质量和正确性按未变身份复用，不能用本次零字节数量
代替子集净收益。下一步冻结有限200K/8K GPU耗时对照并按具体差异归因。
当前默认SHA f356b2c9、发布sidecar422993b0与子集manifest b63b0f25均未改变。

## 可核验身份

原始目录 `.q4t-work/suffix-model-ledger-20261026/`；小收据见
[evidence索引](evidence/mtp-suffix-model-ledger-20261026/index.json)。

| 产物 | SHA256 |
|---|---|
| frozen-plan | `7ed1a8c92fc871daf4e81290daeb90cff1091d605d8c6ac1116723ef2dac7e89` |
| CPU proof | `1a74f64e230575fa118624d6098d2b2781c943f444a2249b05df02e598454c6e` |
| build proof | `7a542bdb18ef7fa40a48d8abaf64b12f4fc7e2d68238bb7176ee0a1d907b90a5` |
| terminal-review | `4e14e08d1e0ef83b28af8cfc0a99861f562dfed89e874477001b46b0347aaabc` |
| final-raw-seal | `99d3c8eb6d8c43739c04944afd2541c63c4998c6e6320436d4e0ecddac400a36` |
| archive manifest | `0d39839e8c84fa7bd6817217a04f256178e98444238a47916d8870f2675762b7` |
| archive seal | `ea482bd0af9bc64f7db314d5da570d7c7f043a4e2720be36b9910d29643525c3` |
