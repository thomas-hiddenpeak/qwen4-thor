# 首份 Nsight 停止原因与证据边界

**本轮停止判据不变：q4t自身的CUDA/NVTX完整性warning尚未闭合。** SQLite导出及q4t、wrapper、launcher退出均为0；不是服务计算报错或export命令失败。诊断检查拒绝6条warning后，`trace_exports_complete=false`，阶段保持`STOPPED`。仅分析已保存文件，没有新GPU、HTTP、nsys执行或重新导出。

## 已证明的范围

实际q4t为PID3181182（globalPid334846354259968），Python wrapper为3181170（334846152933376），launcher为3181142。6条warning中q4t占2条（CUDA/NVTX可能未收齐），wrapper占4条（含没有CUDA/NVTX事件）。wrapper不调用CUDA/NVTX能解释它没有事件，不能消除q4t的两个warning。另有1条事前允许的UM unsupported辅助特性提示。

当前capture在tokenize和请求分配之后、prefill之前启动；handler发送最终响应并显式cleanup后，析构调用cudaProfilerStop。begin/end均error0。命令使用process-tree、`capture-range=cudaProfilerApi`、`capture-range-end=stop`、`kill=none`及`wait=all`；目标收到SIGTERM后正常清理，未强杀。当前源码与冻结快照SHA逐项一致。

77个CPU batch、2716个span、77个flush完整。SQLite内2716个NVTX range全部闭合，并按batch/id、phase、seq/P/T、PID/TID及父子包含关系与CPU记录一一对应。这只覆盖实际发出的插桩范围，不是所有代码或任意工作量完整性证明。

| 已记录类别 | 数量 | 内部核对 |
|---|---:|---|
| CUDA API | 714727 | 已记录returnValue均0 |
| kernel | 266389 | 均唯一关联API |
| memcpy | 4551 | 均唯一关联API |
| memset | 7902 | 均唯一关联API |
| NVTX | 2716 | 全部匹配CPU span且闭合 |

266413个已记录成功launch中，24个没有kernel关联，**全部属于名为“CUPTI worker thread”的TID3181191，并完全落在该线程的CUPTI Buffer flush范围内**。其余266389个应用线程launch均与kernel对应；不能把这24个工具线程launch记为应用kernel丢失。反过来，整对API/activity一起缺失仍不会被这种关联核对发现。

993569条“CUDA collected”恰等于上述API+kernel+copy+memset。与1019188条“CUPTI produced”的差25619中，另有25572条出现在overhead、synchronization和CUDA event表；剩余47的计数类别未闭合。**这不是25619条丢失、也不是47条丢失的证据**，两计数不能直接相减作drop数。

## 尾部与时钟

最后已记录GPU活动于Nsight目标时间12.521370466秒结束，最后应用stream synchronize于12.521550299秒成功返回。`CUDA profiling data flush on cudaProfilerStop`明确记录为12.521866484–12.580175938秒、耗时58.309454ms；`Profiling has stopped`目标时间为12.580528346秒。现有记录没有显示停止发生在这批已记录GPU工作完成之前；也没有“根本没有调用flush”的证据。这些数值仅说明profile收尾，不作为推理耗时或收益。

warning的timestampType=2为**HostTimestamp**，Profiling started/stopped为type1 **TargetTimestamp**。不可把约0.969秒的分析warning与12.58秒的目标区间直接排序，进而断言采集在前一秒已经截断。CPU单调时钟与Nsight区间的共同常数偏移交集也为空，相差45.507µs；本审查只通过键和correlationId关联，并在Nsight自身时钟内检查端点，没有计算精确跨时钟CPU/GPU占比。

capture metadata中的`CUDA_FLUSH_ON_CUDA_PROFILER_STOP=false`与注入info/实际flush区间并存。未取得该字段在本版本/父子进程配置中的解释，不能单凭它判定flush被关闭。

## 可能机制与具体待补

NVIDIA官方资料说明flush和GPU完成是不同问题；forced flush可能包含未完成记录，CUPTI worker也会处理工具自身资源同步与buffer交付。[CUPTI Activity API](https://docs.nvidia.com/cupti/api/group__CUPTI__ACTIVITY__API.html)、[Nsight CUDA排障文档](https://docs.nvidia.com/nsight-systems/UserGuide/index.html#cuda-troubleshooting)。

NVIDIA论坛对**另一份2026.5.1**报告的答复，将同一句CUDA完整性warning归因为forced flush，即使该例预期kernel全部出现；这说明warning不能单独证明应用事件丢失。**本机为2026.1.3.243，未记录内部flush flags/results或drop计数，不能把该解释当成本轮已证明原因，更不能自动豁免NVTX warning。** [官方论坛案例及答复](https://forums.developer.nvidia.com/t/nsight-systems-2026-5-1-produced-112-collected-100-warning-despite-complete-coverage-of-32-kernels/384749)。

下一步应先解决采集工具合同：用一次事先冻结、同版本的最小生命周期观察取得内部最终flush模式/结果、类别计数和drop/terminal证据，区分保守warning与实际缺失；未来可把wrapper诊断单列，但q4t warning仍要闭合。当前已有成功尾部同步和Stop flush，没有证据支持向推理路径补额外同步作为修复。也不应直接扩大buffer、切参数重跑到warning消失。[另一个多进程capture-stop问题](https://forums.developer.nvidia.com/t/question-when-prifilling-megatron-lm/350407)仅提示需要核对进程级finalization，不是本轮的确定诊断。

可保留本报告的原始输出、正常退出、CPU/NVTX逐项闭合和已记录CUDA关联事实。只有nsys8-off这一臂，余下ON及200K profile未执行；**不作新的GPU对照、收益、根因或完整trace接受结论**。正常/CPU分支的保留边界由独立阶段审计确定。本次两个只读脚本均在运行前封SHA，输出位于stage之外，原始文件前后SHA均未变化。
