# 行最大值验收：驱动分配失败的单次有界诊断

2026-11-07。不是性能重试，也不作为新的性能接受样本。原先导和
accept-01的ENV_ABORT保持；默认ea1b474d/3eb46385不变。

## 为什么另立这一阶段

现有2243内存采样/2084查询不能区分驱动分配失败机制，离线阶段已经
停止。现在核对了新的可验证手段：本机kmem:mm_page_alloc事件含pfn、
order、gfp_flags，支持有条件stacktrace及实例内进程过滤/后代继承。
无需安装组件或改驱动，可在本阶段的独立trace实例记录失败分配。
这符合EVALUATION预算外诊断须有新可验证机制的条件；不是重复原HTTP
以寻找通过结果，也不放宽完整五档要求。

NVIDIA公开[595.71.05 mem_desc.c](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/595.71.05/src/nvidia/src/kernel/gpu/mem_mgr/mem_desc.c#L1336)
的1336行是memdescAlloc统一错误上报处；内层可走osAllocPages、IOMMU
映射或其他分配路径。行号不能唯一指向CMA。已安装模块只标TempVersion
r595_00，不能将公开tag视为本机源码的精确身份。公开源码仅用于确定
缺少哪类证据，不作为本机根因或修复证明。

## 固定范围

- 同一个gpu_exclusive完整租约；最多1模型启动、9Chat、16控制、0额外
  CUDA worker、600秒。独立CPU trace控制助手不加载CUDA，另记录命令、
  返回码、身份与输出；所有构建/CPU检查在租约前完成。
- 程序仍为已测34e7ceef，不改运行时；默认MTP完整head/k3，max_seq=1、
  max_prefill=8192、max_len=208896、max_tokens=256，不启媒体或诊断event。
- 原MTP失败启动的1K/4K/8K三个输入，各3次、顺序不变、保留首请求。
  复用accept-01冻结夹具和evalscope请求合同，无新语义提示/随机采样。
- 只在独占租约内创建独有trace实例，限定guard及本阶段后代PID；只捕获
  pfn=-1的失败分配及其栈。256KiB/CPU环形缓冲，记录覆盖/丢失计数；
  不修改顶层tracing配置或其他实例，不加载新模块、不清缓存、不调频。
- 额外只读/proc/buddyinfo与vmstat，沿用原meminfo/NVML边界记录。NVML
  超时仍按原规则ENV_ABORT；不延长期限、不缓存错误为空、不重试查询。
- 结束时先关闭本实例事件并保存受限trace/元数据，再移除本实例；任何
  路径/身份不明只保留现场，不操作外部进程或其他trace实例。

## 区分预测与停止出口

只验证已有H2的驱动特定分配路径：如果NVML再次失败，检查同一查询PID
或本阶段模型PID的失败页分配、order/gfp与内核栈，并与buddy/vmstat及
NVML API时间对齐。只允许按实际调用链确认更具体的分配路径；没有大小、
页阶或调用者证据，不能把CmaFree低值认定为根因。异步内核线程不属于
本阶段PID范围，其遗漏必须声明，不能据未捕获事件证明没有失败分配。

未复现、缓冲覆盖、采样不足或不能对应调用链时均为诊断未决，原
ENV_ABORT不改写，不自动追加模型或样本。若定位到具体环境缺陷，再
决定最小修复及单独冻结受影响验收；本阶段即使HTTP全部完成也不放行
部署，不替代默认MTP五档/质量和真实部署健康。旧包、硬解码、视觉并发
及长稳边界保持。

## 启动前控制读回修正（原失败保留）

env-01在任何模型/HTTP启动前失败，租约3.447秒；追踪实例成功移除、
顶层状态保持，3475文件/3205对象封存。控制检查原先只接受enable=1，
未兼容触发器soft mode的星号。Linux的
[event_enable_read](https://github.com/torvalds/linux/blob/v6.8/kernel/trace/trace_events.c#L1381)
会在soft mode追加星号；修为仅接受1或1*，仍拒绝0/0*/混合或未知值，
并在判定前持久保存完整读回，避免只留下退出后的控制值。

env-02只修这个具体控制缺陷，最大1启动9Chat保持；墙钟扣除首次租约
并取整，剩余596秒，两次合计仍小于600秒。CPU控制助手最多各2次，
合计4次；不增模型或请求预算。本机请求256KiB/CPU会读回258KiB，
14 CPU合计3612KiB，按实际记录，不宣称精确256KiB。

## 实际结果与停止出口

env-02来源b1a7e8b9，候选ELF仍为34e7ceef。独立重建3455项冻结输入及
2323个Git来源，封存3547路径/3273对象；[分析](evidence/vision-rowmax-20261107/env-diagnosis.json)、
[绑定](evidence/vision-rowmax-20261107/env-diagnosis-bindings.json)及
[退出观察](evidence/vision-rowmax-20261107/env-diagnosis-postflight.json)保留。
本阶段不是完整验收，**结论为复现NVML init阻塞，根因未决**。

- 实际1模型启动、9预留Chat中7收到，6完成/1取消/2未发出，控制和额外
  CUDA worker均0。1K/4K数据库各3条，8K数据库为空；完成请求的输入、
  256输出token和length终态核对通过，不将诊断计时用作性能接受。
- 租约145.314秒，连同env-01共148.762秒，小于原600秒上限。CPU控制
  助手各2次，两轮合计4次；没有自动追加模型或请求。
- NVML共121次查询，最后worker 4132550在07:09:20.429左右进入init，
  5秒期限及1秒回收后仍未完成，guard记ENV_ABORT，驱动FAILED。模型
  waitable rc=0、正常shutdown；custody/reaped仍缺失，不能补造成功回执。

追踪有效读回为1*，独立实例成功移除，顶层配置不变。保存1,399,439字节，
没有文件截断；CPU9环形缓冲覆盖449条，commit overrun/dropped均0。
留存2491条失败页分配、2492条栈记录，不能据此计数全程失败分配。
其中2489条order=9为透明大页匿名缺页路径；两条q4t线程order=11/12
沿nv_alloc_pages、osAllocPagesInternal、memdescAlloc和kvmalloc_node，
对应4KiB页下8/16MiB连续分配尝试，分别早于失败init约38.323/0.932秒。
这些是页阶对应的分配尝试大小，不是已经确认的业务缓冲大小。

不能将这两条记录判作根因：Linux
[kvmalloc_node](https://github.com/torvalds/linux/blob/v6.8/mm/util.c#L580)
可在连续分配失败后回退vmalloc；本次未捕获回退最终返回。失败NVML
worker自身只留存6条匿名透明大页失败，没有其驱动阻塞调用栈。追踪
还排除了无关异步内核线程，存在覆盖；这些限制均阻止因果结论。
本机事件格式将原始pfn=-1打印为0，过滤基于原字段，并非误采pfn=0。

失败init前0.306秒的内存采样为MemAvailable 19.589GiB、MemFree
13.570GiB、CmaFree 65.668MiB、SwapFree 0；前0.609秒的buddyinfo中
DMA/Normal的order>=11均为0。大块连续分配压力确实可见，但这些快照
既不能证明最终分配失败，也不能建立CMA准入阈值。07:06:50～07:09:45
内核窗口仅有12条I2C超时，没有NVRM/NV_ERR/OOM记录；不得把原accept-01
的NV_ERR_NO_MEMORY移植为本轮实际报错。

只读后验确认9个自有进程身份已不存在，GPU计算PID为空，18146与原
锁inode3294067可用，自有trace实例不存在。默认ea1b474d/3eb46385
保持；这不修复原ENV_ABORT和缺失custody，也不意味着环境故障已解决。

已触发本计划“覆盖或不能对应调用链即未决”的停止条件，不自动扩大
缓冲、追加同类请求、延长NVML期限、降低模型容量或重启设备。后续需要
在独立冻结的诊断范围内取得**阻塞NVML worker的内核等待栈及驱动分配
回退完成结果**，才能判断分配失败、回收等待或驱动锁等待；目前没有
可据此实施并验收的最小修复。公开驱动源码不是已证明的本机精确身份。
优化候选与回退身份保留，PR #20继续草稿；完整默认MTP五档/质量及部署
健康尚未完成，整体优化交付仍未完成。
