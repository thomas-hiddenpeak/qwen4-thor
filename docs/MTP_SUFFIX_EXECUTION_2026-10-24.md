# 固定 k3 suffix 执行区间定位（2026-10-24）

## 开测前冻结

用户授权补充 prefill 与神经草稿执行/提交观测，定位子集200K及8K下降，
并复核两种head的重复收益。本轮是性能优化研究的有界诊断，不作优化交付；
沿用单流greedy、k=3、min32准入及完整全局token ID，不改计算、状态提交、
接受、尾部回退、词表映射或任何同步顺序。默认交付f356及显式BF16子集保持。
旧有效/NO_GO/ENV_ABORT结论均不覆盖。入口源码9253e714；参考研究程序6a4d3d62，
参考运行时源码cb0fa73c。完整身份随冻结plan与构建记录保存。

一个观测候选；必要构建后第一项测试为tools/evalscope真实HTTP E2E。
所有新开关默认关闭；Q4T_EXEC_DIAGNOSTIC=1记录线程CPU/单调墙钟，
Q4T_EXEC_NVTX=1才打开NVTX；Q4T_EXEC_PROFILE_INPUT精确选择8170或204781
输入的一次请求，调用cudaProfilerStart/Stop。CPU-only观测不启用旧MTP/LAYER计时。
不新建CUDA event，不加同步。每观察线程只在启用时分配4096条固定记录，
超容量、未闭合记录、时钟失败、trace缺失/丢失都使诊断证据不完整；不能静默截断。
缓冲在已有完成点后输出，flush成本单独记录；观测样本不能当正常性能结果。

观测区间包括目标prefill逐块、PLE已有reader IO/scatter计数、H2D提交、
最终readback与commit、prompt draft extend及其位置回读、MTP每步两个草稿token、
attention、MoE counts pageable D2H/stream同步、expert/head提交、verify、
checkpoint恢复、接受前缀extend及next_d0/next_g。父子记录不可重复累加。
墙钟减线程CPU只是non-running wall；CUDA API调用可阻塞，不能叫纯提交。
Nsight CUDA runtime correlation与NVTX绑定，计算kernel/memcpy/memset区间并集；
GPU流经过时间、实际活动并集和排队延迟分别报告，不把空隙直接归因为CPU调度。

## 固定预算与顺序

49次生成HTTP、13次新服务、整阶段7200秒（含profile导出/收尾）；0额外warmup、
0重试、0条件优化B、0健康/部署请求、0直接GPU合同测试。必要编译只生成一个
观测候选，编译失败可修复但不产生多候选性能择优。第一测试前冻结程序SHA、
全部运行时/工具依赖、夹具、监控、子集manifest、模型metadata及请求顺序。
整阶段驱动持共享独占锁，失败/竞争/监控失败停止自己的进程并保留现场；
监控失败记ENV_ABORT，不能以HTTP完成或轮询未见竞争替代环境闭合。

| 顺序 | head/suffix | 内容 | HTTP数 |
|---|---|---|---:|
| 1 | 完整 ON，无观测 | 新8K首HTTP gate | 1 |
| 2–3 | 完整 OFF/ON，无观测 | 重复代码3、文本3 | 12 |
| 4–6 | 子集 OFF A1 / ON B / OFF A2，无观测 | 8K→200K→代码3→文本3 | 24 |
| 7–9 | 子集 OFF A1 / ON B / OFF A2，CPU观测 | 8K→200K | 6 |
| 10–11 | 子集 OFF/ON，Nsight | 仅8K捕获 | 2 |
| 12–13 | 子集 ON/OFF，Nsight | 8K前置请求（不捕获）→200K捕获 | 4 |

每请求固定输出256，逐请求核对旧同head完整输出SHA、实际输入数、finish=length、
释放/取消生命周期与自有退出。材料全部是已见开发诊断材料，不冒充独立验收集。
保持自然继承页缓存、原启动auto；不drop cache、不改功率/频率/亲和、不额外预热。
监控沿用已修复工具与11项CPU合同证据，保留最后服务退出后的完整采样及flush/join屏障。
Nsight固定本机2026.1.3，cuda/nvtx trace、无CPU采样/上下文切换采样，
cudaProfilerApi capture、capture结束仅stop，target与launcher退出独立核对。

重复保护目标：完整head代码/文本各median ON/OFF≥1.05；子集代码/文本
B/A1和B/A2均为正收益，共享B描述性几何均值≥1.05。共享B及启动内重复不当
独立启动。8K吞吐、200K TTFT、工作量/接受分布及资源全部报告，不设事后容差，
不以重复收益抵消下降；本轮没有五档优化接受或统计非劣声明。

待检机制按预先区间区分：200K差是PLE/readback/CPU提交还是GPU活动变化；
8K是神经草稿CPU/API等待、GPU活动或轮间空隙。检查首次shape/尾chunk计划调用、
pageable counts D2H、metadata等待及上下游关联；机制定位不等于修复验证。
预算结束分别写证据有效性、测量完整性、统计结论和工程决定；没有具体证据
则停止，不能追加profile/参数扫描直到出现收益。本轮不自动启动运行时修复候选。

旧22质量/86host/10阶段合同只在逐依赖静态审查、未改变计算与状态的原范围复用；
不声称新观测程序完成整模型质量证明。原接受前缀oracle仅两GDN层的范围保持。

## 执行与结果

**本轮 ENV_ABORT，测量不完整，保留默认交付版。** 2026-10-24
07:49–07:51 CST，完整阶段持锁126.237545秒后按监控失败停止；不重试、不补采。

| 项目 | 实际身份/结果 |
|---|---|
| 运行时源码 | `55363f18dedb947ac52e69e10475d3082cb71a82` |
| 测量工具提交 | `7c3984a` |
| 观测候选 | `f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914` |
| 冻结plan SHA256 | `a145dafe688ef60ec117d76fb5b3d6116a39b22bf92c412aede811c45e5df47b` |
| 必要构建 | Release/g++14/CUDA110a，q4t目标成功，零warning/error |
| 实际服务/HTTP | 2/13启动，4/49完整响应；state只登记1条已完整闭合组 |
| 开关开启验证 | CPU观测、NVTX、Profiler、Nsight、离线解析器均未运行 |
| 证据目录 | `.q4t-work/suffix-execution-20261024/diagnostic-v1` |

第一项测试是关闭观测的真实HTTP：完整head suffix ON新8K通过，完整输出
SHA、输入8170、输出256、finish=length、请求释放及服务退出码0符合合同。
第二服务完整head OFF完成3条重复代码，全部输出SHA/输入744/输出256及
请求结束释放一致。重复文本只有客户端批次启动记录，0条已观察到的服务
admitted/DB/SSE；没有未完成文本响应可作为样本。第二服务有shutdown complete
日志，但wrapper exit/startup finalized记录缺失，不能伪称其完整退出合同通过。

失败为监控读取自有evalscope客户端 `/proc/3164108/io` 时open EACCES，
start_ticks=98952521；q4t服务PID是3163321。26条历史同身份采样均OK，
最后一次该客户端为R、34线程、io可读。失败attempt结束后12.902ms，父进程
记录该客户端returncode=0；此时间是父进程返回后的日志，不是精确内核退出时刻。
这支持客户端退出窗口假设，但没有证明具体内核权限原因。

旧修复仅处理pinned目录确认同身份Z/X退出。新失败的控制流表明复查stat
读取/解析成功，但“同身份且Z/X”合取未满足；监控在该失败出口没有保存
初次stat与复查stat，所以不能断言哪一项未满足，也不能用前一秒的R或稍后的
rc0回填失败时状态。未放宽监控或吞掉EACCES。本轮没有CPU复现/监控修改。
下一步具体诊断应补全所有失败出口的初次/复查原始状态及时间，再为退出窗口
制定有界的同身份证明与CPU合同；本轮不自动开启新验证阶段。

127次监控尝试126次flush，1次未flush；118条历史NVML查询正常，未记录外部
竞争。内层state/postflight为ENV_ABORT；外层guard为COMMAND_FAILED/child1并
释放锁，不能把二者写成同一状态。审计时7个历史PID+start_ticks均消失，3个
历史自有PGID无成员。仅停止自身进程，未改频率、亲和或缓存策略。

782冻结文件、69原始seal文件、439源码快照（含patch）、197模型metadata
一致。离线审计v1复用旧接口时多加timing字段，造成精确字典比较误报；v2
改为只读兼容视图，errors=[]。原v1、修正脚本及v2均保留，没有改原始证据或
增加HTTP。两个分析器只经过AST/静态审查；没有执行结果可称解析器验证通过。

| 决策字段 | 结论 |
|---|---|
| 证据有效性 | 原始内容身份PASS；阶段环境ENV_ABORT，不能用于性能接受 |
| 测量完整性 | INCOMPLETE；4响应，重复对照、子集200K/8K及CPU/GPU定位未完成 |
| 统计结论 | 没有完整配对，不判断200K/8K下降、收益保持或真实差异 |
| 工程决定 | STOP_ENV_ABORT_KEEP_DELIVERED_F356；保留观测候选，未部署、未验证修复 |

旧有效轮次完整head重复代码/文本+14.21%/+14.54%、子集共享B描述性GM
+6.80%/+6.28%仍是旧身份、旧材料及原范围证据；本轮不能更新为新程序收益。
旧22质量/86host/10阶段合同只按静态复核的未变数学/状态依赖说明继承范围，
不代表本轮重新通过，更不代表诊断开启路径通过。默认f356、原发布sidecar、
显式65536行BF16子集及6a研究程序保持，其他旧NO_GO/ENV_ABORT均不改。

精简证据见 [evidence目录](evidence/mtp-suffix-execution-20261024/README.md)。


## 阶段封存

独立归档 `809` 条目、`39149556` 字节，逐文件拷贝并独立回读通过；
目录 `.q4t-work/suffix-execution-20261024/archive-closed` 保留本轮观测程序、
6a参考程序、f356交付程序及发布元数据、子集manifest、原始失败、源码快照、
构建元数据、审计修正链及尚未执行的离线工具。原路径保留，未替换默认程序。

- archive manifest SHA256：`53d2a41150cb0fcaf8cdad86eba0a895143fb386e0bb3e0c179aa74a48c8697b`
- archive seal SHA256：`1ad488f15ba971f8a78f1e55d03a14bbbd7183ca1933ef5c3d04a6e62d153e8c`

本阶段按授权提交推送工作分支codex/suffix-k3，保留独有研究提交及原证据路径；
提交/封存不是验收通过，不清理或覆盖旧失败与旧有效证据。
