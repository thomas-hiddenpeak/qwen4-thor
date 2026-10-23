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

待必要构建、冻结身份及完整持锁验证。证据目录：
`.q4t-work/suffix-execution-20261024/diagnostic-v1`。
