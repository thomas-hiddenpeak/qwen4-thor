# 完整热缓存启动的物理回收时间轴（2026-10-20）

## 目标与冻结范围

用户授权优先定位驱动页池和物理回收，先补内核时间轴，有明确证据
再修复及对照。属于可靠性任务；默认19ea及NVML五秒时限保持。
复用已验证runtime observer 2a66ee2f与完整逐文件缓存门，不再改VMM。
本轮完整热指同一expert文件集合67987791872 bytes启动前全驻留，
其他模型payload零驻留；不是要求加载全过程文件缓存绝不被自然回收。

两个机制假设：

1. 驱动页池持有可回收页，但回收未及时发生或回收量不足，造成
   后续设备分配直接回收/规整；池是否参与扫描及扫描返回量可区分。
2. 物理页阶数/分布或分配时持锁造成长尾；将compaction请求order、
   buddy分布、应用分配与NVML栈按monotonic关联。并行记录memcg，
   不把全机压力与64GiB cgroup限制混为一谈。

先完成工具和一次CPU空载短合同（无模型/GPU请求），然后仅一次
完整热准备及最多一次模型加载/1K质量。全阶段独占lease，准备门
失败则模型加载为0，保留准备时回收时间轴，不补读或降低缓存量。
有具体可修复机制才另行冻结最多一次干预对照；若成功并提出修复
接受，最多再一次正常退出后的热重启合同。没有证据不消耗后两次。
禁止循环搜索缓存量、放宽监控、全局drop_caches或盲改驱动参数。

## 采集与有效性

本机CONFIG_SHRINKER_DEBUG/KPROBES/FUNCTION_TRACER未启用，采用现存
静态tracepoint。新建独占命名tracefs instance，仅更改自身实例；
mono时钟、有限每CPU缓冲和文件上限，保存事件format、计数及丢失。
捕获slab shrink、direct/memcg reclaim、compaction和order>=4页分配。
NVIDIA扫描回调从本机已加载模块身份、ELF符号/重定位定位，不用
公开参考tag冒充本机精确版本。页池对象数的单位是对应order页块，
缺少池order关联时只报告对象数，不能乘4KiB冒充物理字节。

外层supervisor全程持同一GPU lease；trace及资源观察器为guard兄弟
进程，因此覆盖ENV_ABORT后的本任务清理。资源0.2秒，文件驻留1秒，
包含meminfo/vmstat/buddyinfo、PSI、模型/NVML栈及已发现模型memcg。
最多360秒，原始kernel文本最多512MiB，资源最多256MiB。采集器
失败、丢失、达到上限或清理失败均显式记录，不据不完整轨迹下阴性结论。
trace开启前后与应用CLOCK_MONOTONIC对齐；插桩不是性能测量。

正常启动才发送既有1K质量夹具；ENV_ABORT不宣称HTTP通过。封存
身份、原始事件、启动/清理状态、文件向量和分析；只按证据确认
机制，不把相关性升级为已证明锁持有者或通用驱动缺陷。

## 工具合同与本机能力修正

独立collector短检查通过：内核无丢失、实例删除、全局配置不变；
资源11个系统样本/3次完整非faulting驻留采样。资源v1曾因缺少PSI
失败，本机/proc/config.gz明确CONFIG_PSI未编译；修正仅把该证实
不支持的接口标unsupported并保留原始错误，不填零，其余必需读取
仍失败即停。该修正后联合CPU短检查通过，无GPU/NVML请求。

本机驱动模块build-id/srcversion/命名函数地址三重一致，scan回调
从init重定位提取为.text+0x18a80。trace的显示符号是
nv_free_user_mapping+0x568/0x764 [nvidia]，已还原到同一绝对地址；
前置命名符号自身仅4字节，此名称不能按字面解读。解析v2曾因
AArch64映射符号歧义拒绝，按本机kernel header核实的过滤规则修正
后v3通过，失败保留。工具共27项host检查通过。

准备运行冻结的baseline-v1。guard工作墙钟上限240秒，另留清理
余量，collector仍360秒；监控时限未改。外层SIGTERM进入清理，
若清理超时/未证实则写共同recovery-required标记，禁止后续lease
在未恢复状态下继续GPU工作。新增模型加载仍0，默认未改。

## baseline与唯一机制对照

baseline-v1完整热向量通过并实际加载；guard因NVML compute_v3查询
超时ENV_ABORT（本轮init与handle都完成）。内核393193事件全部解析、
0丢失，观察至自身scope清理后。1392笔设备及99笔host分配成功、0free；末一笔
40MiB分配未闭合，无CUDA失败返回码，无HTTP结果。

长等待前KReclaimable由52.34GiB降至约0.67GiB，memcg仅约12/64GiB且
events全零；不能再指称大页池始终扣留不归还。1424个NVIDIA shrinker
区间最久0.368ms。两个160MiB分配各约4.97秒，其中规整约4.85秒，
每笔80次order9成功页分配，实际粒度2MiB。第一笔末尾跨runner清理
边界，第二笔全部发生在该边界后，不能当两笔完整的故障前耗时样本；清理前的40MiB
及第一160MiB已观察到相同机制。退出后misc reclaimable回升约50GiB。

该证据支持唯一条件对照precompact-v1：重新建立原完整热向量后，
在任何模型分配之前对node0/compact写入1一次，再次要求原完整向量
完全一致，不补暖；仍原runtime observer、C256/64GiB/noMTP/1K质量。
本机仅一个内存节点，操作影响全机物理页布局，并非更小影响面的
定向页池清理。没有持久内核参数改变，但不能精确回滚原物理布局。
保存操作前后buddy/vmstat与内核事件，返回成功不等于高阶页准备成功。

预测：若order9以上空闲页增多且完整热向量保留，能检验把规整移到
模型分配前是否足以避免驱动调用长等待。若仍失败，否决这一次前置
规整合同，不增加反复规整/换参数/降低热缓存量。跨阶段驱动池与
后台状态不完全一致，因此即使通过，也只按实测条件作工程判断。
最多增加1模型/1HTTP；若失败本轮停止干预。默认仍不变。

## 最终结果与工程决定

唯一前置规整对照仍ENV_ABORT，不接受为完整热启动修复。规整写入
完成约0.0995秒，trace确实记录其两个zone的规整；原完整逐文件
驻留向量在操作后继续通过。Normal区order>=9空闲容量由
2785017856降到2778726400 bytes，少6MiB，未建立额外高阶储备；
DMA区有所改善不能代替设备分配实际需要的Normal区供给。

| 项目 | baseline-v1 | precompact-v1 |
|---|---|---|
| 完整热起点 | 67987791872 bytes，逐文件精确 | 相同，操作后再核验 |
| 模型/HTTP次数 | 1/0 | 1/0 |
| guard | ENV_ABORT | ENV_ABORT |
| NVML最后未返回API | compute_v3[0] | handle[0] |
| 内核事件 | 393193，无丢失 | 834618，无丢失 |
| 系统/驻留快照 | 232/60 | 224/57 |
| memcg峰值 | 约12.443GiB | 约12.293GiB |
| memcg max/oom事件 | 均0 | 均0 |
| 本任务scope/trace实例 | 已清理 | 已清理 |

precompact中NVML init耗3.981秒后成功，handle开始未结束；5秒限制
作用于整次查询，不把它解释为handle单API已经阻塞5秒。模型此次
到最终2348023808-byte workspace cudaMalloc未返回；清理前同模型
线程累计6.847秒规整，其中workspace未闭合的2.259秒观测窗口内
规整2.001秒。两次均没有取得HTTP结果，无质量接受主张。

**已确认的机制**：驱动控制查询等待与设备分配中的高阶页规整同窗；
同线程事件覆盖长cudaMalloc的主要耗时，实际order9代表2MiB物理块，
不是整笔160MiB或workspace必须物理连续。文件缓存回收提供空闲字节，
但不等于及时提供所需阶数的空闲块。失败查询窗口baseline的19次
Normal区样本order>=9全零；precompact的18次中15次全零，其余最多
3个order9块，order>=10恒零，此时全机空闲字节最高约10.60GiB。
所捕获的NVIDIA shrinker扫描
阶段区间均短；该区间不含前置count_objects，不能概括成所有池锁
从未等待，更不能由end的最后一次scan retval计算区间总回收字节。

**排除与保留**：本次长停顿前约52GiB KReclaimable已降至不足1GiB，
所以“仍有几十GiB驱动池扣留，直接清掉池即可”不符合本次时间轴。
misc reclaimable随模型退出回升约50GiB支持驱动池生命周期解释，
但没有逐池order/字节归属，不宣称全部KReclaimable都是NVIDIA。
memcg远未到64GiB且无max/oom事件；物理状态不一致、插桩和背景负载
仍是对照限制。RM锁完整持有者及驱动实现缺陷未证明。

按冻结预算共2模型、0HTTP；两阶段487份封存文件SHA全部复核。
最终40项host合同通过，离线分析补输入封存SHA与输出路径保护，
资源单位从封存页大小派生，两阶段复算结果一致，无重复GPU验证。
两collector正常完成、trace丢失0、全局trace配置不变，无残留scope。
默认19ea摘要未变，无持久内核参数变更，无运行时补丁或默认部署。
诊断状态为BOUNDED_DIAGNOSIS_COMPLETE_NO_FULL_WARM_FIX；停止本轮
规整干预，不因工具/分析完成宣称热启动修复。现行可靠启动仍使用
已接受的定向模型缓存回收合同。见[机器可核验审计](evidence/warm-reclaim-20261020/audit.json)。

后续最小可验证方向是**设备分配与pinned host分配的启动顺序**：
实际轨迹及源码存在逐层device→L2/mirror pinned交错，全部99笔
host分配完成后才分配trunk/workspace。可另立独立原型先预留完整
设备分配、再建立pinned缓存，验证是否避免后期高阶规整；当前只
是有依据的假设，未证明pinned造成碎片，未实现也未追加加载。
单纯把workspace挪到最前可能只移动压力发生点，不等价于该原型。

独立机制参考：本机ELF/加载身份负责callback归属；
[Linux v6.8 shrinker](https://github.com/torvalds/linux/blob/v6.8/mm/shrinker.c)
解释扫描区间与最后返回值，
[Linux v6.8 compaction](https://github.com/torvalds/linux/blob/v6.8/mm/compaction.c)
解释node0操作，
[NVIDIA r595参考](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/595.45.04/kernel-open/nvidia/nv-vm.c)
仅辅助理解按order复用页池，不冒充本机TempVersion精确源码。
