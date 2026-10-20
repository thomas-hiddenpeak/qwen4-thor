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

## 首轮证据与第二轮变量冻结

diagnostic-v1受控只读预热67,987,279,488 bytes，cache端点68,717,608,960
bytes。守卫39次查询，最后CLEANUP_FAILED约6.007秒；阶段ENV_ABORT。
内核栈直接观察模型线程nv_alloc_system_pages → __alloc_pages →
try_to_compact_pages/migrate_pages，NVML线程rmapiLockAcquire →
os_acquire_rwlock_read。模型最终完成model_load 23,706ms并启动HTTP，
随后被守卫清理；本轮kernel没有NV_ERR_NO_MEMORY记录。
因此初始化超时不要求先有分配失败；单凭旧报错不能断定模型已OOM。
全机compact_stall增959、compact_fail增545、compact_success增414，
这些是系统背景计数，不冒充模型独占计数；模型栈提供归属证据。

历史v3精确kernel时间1792490496.375145，晚于wrapper的清理SIGTERM
1792490495.629844约0.745秒。报错可能与清理中断有关，未证明，不能
沿用“分配失败先导致NVML超时”的确定因果。原日志保持不改。

第二轮仅改变加载前定向回收：同预热文件/64GiB上界、同模型/夹具/
memcg/监控/observer，新增--clear-model-cache并要求payload零驻留门。
最多一次模型加载与1K质量，不新增性能样本；原阶段v5的中断合同复用。
若通过，建立明确的卸载研究启动cache合同，不宣称驱动内部已修复，
不偷偷在请求间回收缓存，也不接受继承缓存启动模式。

## 对照准备失败与定向工具加固

diagnostic-v2未加载模型：单次FADV_DONTNEED无错误，但一个expert文件
仍驻留393,216 bytes，严格零驻留门拒绝启动，guard COMMAND_FAILED。
没有NVML超时，没有第二次GPU加载，不将准备失败算成对照通过。

新增prepare_target_cache：最多3次建议/观测，已知正残留才继续，间隔
0.1秒；未知观测或系统调用错误立即停止，零残留立即完成。逐次完整
观测写cache-preparation.json，原零驻留门与cache-after-advice保留。
这处理advisory调用不能保证一次完全回收的情况，不声称已证明残留原因。
6项host验证零/暂留/持续残留/未知/调用错误/预算边界全部通过。

diagnostic-v3只重验受影响的缓存准备并完成原定第二次加载，预热与
GPU参数/监控不变；不增加第三次GPU加载或性能样本。若持续残留仍拒绝。

## 有界验收与工程决定

diagnostic-v3完成：69次守卫查询全部OK，最长0.08017秒；1条1K HTTP
输出精确匹配，runner/monitor exit0，无清理失败；152份文件封存复核。
首轮126份、准备失败轮98份也完整复核，共376份，失败原样保留。
新轮实际使用1次回收建议即达到零驻留；2/3次分支由定向host验证覆盖，
不声称本轮实机触发了额外pass。两个真实模型加载预算用完，不追加。
详见[evidence审计](evidence/nvml-load-20261020/audit.json)和
[同样本等待栈](evidence/nvml-load-20261020/wait-witnesses.json)。

支持的关系：模型NVIDIA分配路径直接规整/页迁移，与NVML RM API
读锁等待同时出现；初始化超时无需先发生ENOMEM。本机分配/规整路径
造成驱动接口等待是当前最强解释，但锁持有者没有通过lock tracing
精确绑定，不能声称已证明全部驱动内部因果。一次报错晚于清理信号，
不能再把它作为故障起点。没有证据要求更换NVML或改成常驻查询线程；
compute查询同样走驱动控制路径，不能保证绕过该锁。

操作合同：后续真实offload策略实验使用显式--clear-model-cache，
64GiB memcg、无交换，启动前最多3次建议/观测且payload必须零驻留；
不修改模型内容，不drop全机缓存，不在请求之间回收。父版/候选均按
同一启动与请求历史协议，冷启动与服务内热请求分别记录。热缓存直接
启动仍未接受，NVML门保持；完整物理RAM仍不可认证。

此次可靠性工程收尾接受的是上述明确处理合同与缓存准备工具加固，
不是热启动/驱动内部缺陷已修复。NVML保持默认，失败仍ENV_ABORT。
默认运行时19ea不变，无需重新构建部署；工具前版61b3162的独立回退
副本及SHA保存在.q4t-work/releases/nvml-load-20261020，必要时只回退
该工具，不覆盖其他任务。沿用未变scope终止/身份合同证据，新正常
HTTP覆盖改变的前置cache步骤。后续优化不宣称继承缓存启动已恢复。
