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
