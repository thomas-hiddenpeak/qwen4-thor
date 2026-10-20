# NVML初始化与加载分配失败诊断（2026-10-20）

## 目标与分类

用户授权优先解决该可靠性问题，具备条件直接收尾，再推进缓存策略。
目标是区分NVML调用生命周期、模型加载资源压力与文件缓存归属影响，
修复已证实缺陷或建立经过验收的处理合同；不预设NVML自身有缺陷。
不延长监控超时、不静默回退、不用旧样本填补未知，不为统计反复补测。
随后缓存优化属于独立性能任务，先离线筛选，再按HTTP优先及五档规范。

## 已有证据复用

默认运行时19ea4e57（源码166a535）与scope生产工具未变。
基础阶段v2/v3/v4分别17/14/11次guard查询，末次TIMEOUT或CLEANUP_FAILED，
最后API边界都为init。v3内核同期出现NV_ERR_NO_MEMORY，不能直接证明
它来自模型线程或NVML线程。v4将memcg从16GiB增至64GiB仍失败。
v5定向回收模型文件缓存后52次守卫查询全部OK，最长0.0851秒；
1条HTTP质量及GPU中断清理通过。原封存与失败不覆盖、不作为新PASS。

本机pynvml.nvmlInit实际调用nvmlInitWithFlags(0)，不是旧nvmlInit_v1。
[NVIDIA初始化文档](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlInitializationAndCleanup.html)
说明该入口延迟初始化并要求配对shutdown；不能据函数名认定使用旧API，
也不能从文档推断本机驱动不会阻塞。驱动报告r595_00 TempVersion，
外部不同版本的问题只能参考，不能代替本机因果证据。

## 首轮冻结预算

1. 只读核对已有三次失败与一次成功的时间轴、真实调用入口、kernel记录。
2. 新增一次受控热缓存加载：同19ea、C256、64GiB memcg、原1K质量夹具。
   先只读顺序读取expert safetensors，最多64GiB/180秒，记录文件及
   实际cache端点。该预热不是精确重演历史缓存布局，也不是性能试验。
3. 全阶段使用默认NVML GPU lease。守卫5秒、资源监控2秒门不变。
   额外只读observer每0.2秒、最多300秒记录/proc/meminfo、vmstat，
   目标模型与NVML worker主线程及D状态线程的内核stack/wchan；每进程
   最多16线程。sudo仅用于读取内核栈和日志，不修改内核/设备参数。
4. 本轮模型加载最多1次，runner1200秒，ENV_ABORT即终止；不自动重试。
   若出现具体新证据，先记录第二轮定向变量与验收清单，再做一次对照。
   没有新机制证据不继续扩大GPU采样；保留未知项。

观察器可能改变时间，因此仅作因果定位，不能当性能结果。旧五档基线
与已通过质量不重跑。诊断入口tools/verify/inference_metadata/diagnose_nvml_load.py，
证据.q4t-work/nvml-load-20261020/diagnostic-v1，全阶段由seal_stage封存。

## 出口

要求明确证据支持的关系、尚未证明的根因、具体可操作处理边界；
若修改生产工具或运行时，验证失败/清理合同与受影响HTTP质量。
未修复的热缓存问题不得用冷缓存通过冒充已修复。缓存策略真实优化
不能跨过该可靠性出口；离线筛选可在不依赖GPU的范围进行。
