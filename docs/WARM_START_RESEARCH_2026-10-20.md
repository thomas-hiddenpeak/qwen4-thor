# 热页缓存启动有界调研（2026-10-20）

## 范围与出口

用户调整当前目标：优先热页缓存启动可靠性，暂缓真实共享缓存实现
和性能采样。允许复用既有证据；仅修复具体缺口。出口为有证据的修复
或明确环境限制及经验证操作合同，不声称一定修复驱动内部行为。
本轮不部署诊断包装器，不改变默认19ea、NVML时限、scope隔离规则。

## 首轮只读核对

既有diagnostic-v1证明模型分配规整与NVML读锁等待同时出现，但无
应用API/分配大小；MemAvailable仍约65GB，不能据可用总量排除
连续物理页分配压力。C256每层GPU gu_packed 419430400 bytes、
dn_packed 209715200 bytes；L2 pinned 52429056 bytes、mirror pinned
22118528 bytes；最终workspace另有大分配，不能直接假定L2是元凶。

本机nvidia.ko与已加载模块srcversion均01406A18AAA4C43F4A8F257。
反汇编nv_alloc_system_pages+0x238对应__get_free_pages调用的返回点，
页order由分配描述符动态读取，现有栈不能反推具体order。系统参数
EnableSystemMemoryPools=529，另有大量KReclaimable；后者不能全部当
文件页缓存或进程泄漏。没有修改驱动参数、全机drop_cache或内核配置。

独立参考只解释机制，不冒充本机完全匹配源码：
[NVIDIA r595参考nv-vm.c](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/595.45.04/kernel-open/nvidia/nv-vm.c)
含页池及带order的系统页分配；
[Linux内存概念](https://docs.kernel.org/admin-guide/mm/concepts.html)
说明连续页需求与规整的关系。本机TempVersion不等于该发布tag。

## 冻结新增诊断预算

先做一次、且仅一次受控热启动，复用原64GiB只读expert预热、C256、
L2=16、mirror=8、64GiB memcg、no-MTP及1K质量夹具。单一默认NVML
lease，5秒守卫/2秒资源查询不变，ENV_ABORT即停，不自动重试。
观察器0.2秒/300秒，增加buddyinfo/pagetypeinfo；runner上限1200秒。

独立诊断binary仅链接包装cudaMalloc/cudaHostAlloc/cudaMallocHost，
记录调用前后monotonic、bytes、返回码、调用点偏移；分配参数及返回值
不改。日志写失败退出125，不能默默丢掉因果证据。包装会影响时序，
本轮不是性能样本，也不能用一次未复现证明修复。

复用foundation构建对象，无包装重新链接SHA必须精确等于19ea；
已核验通过，再加入包装器得b0f68b55。初次误用build旧对象被SHA门
拒绝，未进入GPU；默认程序未改变。必要构建首次write返回值告警
已修正，最终-Wall -Wextra -Werror通过。
证据目录.q4t-work/warm-start-20261020，阶段结束后封存。

有新的具体机制证据才冻结最多一次定向对照；没有则收敛限制，
不盲目循环加载。不把先清零页缓存的既有通过称为热启动修复。

## 首轮定位及唯一对照冻结

diagnostic-v1 ENV_ABORT，模型第47次400MiB专家gu分配（顺序layer46）
耗时5.41098秒、API最终返回成功；地址映射MoEResidency::Init的alloc。
五个同窗口采样同时见模型规整与NVML RM锁等待；不是pinned L2分配。
MemFree约4.39GiB但MemAvailable约66.88GiB。尚不能确定请求的内核页order
或锁持有者；不据此把连续400MiB物理块作为API要求。128份文件已封存
（最终数量以审计输出为准）。没有HTTP结果，不追认模型质量通过。

这支持一次定向对照：保留同样预热，只对字典序前缀完整expert文件
执行至多16GiB定向回收，加载前确认被选文件零驻留且全模型payload
仍至少32GiB驻留。其他参数/诊断包装/NVML规则不变；最多一次加载和
1K质量，无自动重试。本轮用于区分“必须全清”与“留出部分分配余量”，
不是证明16GiB为通用阈值，不改生产工具的严格零驻留门。
两次加载预算用完后收敛结论，不继续搜索最小清理量。

## 对照结果与本轮工程收尾

唯一对照diagnostic-v2仍ENV_ABORT。完整前缀expert文件16996773792
bytes经一次建议后零驻留；加载前全模型仍驻留54050951168 bytes。
专家权重分配通过，随后LoadModel最终workspace的2348023808 bytes
cudaMalloc只有begin，没有end。25个后续样本同时见模型规整与NVML
RM锁等待；不能给这个未返回调用编造完成时长或OOM返回码。

两轮共258封存文件全部SHA核验；观察器host伪后端验证三种API的
参数、输出指针及成功/失败返回值原样转发，6条begin/end记录。
二者均未取得HTTP质量结果。默认19ea SHA保持，无残留q4t scope，
不部署诊断binary，不新增性能样本；旧正常质量和全回收启动证据仅
按原身份/范围复用，不能称这两次诊断通过了运行验收。
见[可复核审计](evidence/warm-start-20261020/audit.json)。

**已定位的具体机制和边界**：保留大量模型页缓存时，大块设备分配
触发内核物理页规整，驱动控制接口同时被RM锁等待阻塞，超过现有
监控时限。不只是L2 pinned分配，也不局限于一层专家权重；部分回收
能改变发生位置，但本次仍未恢复可靠启动。第一个超时分配本身最终
成功，故“NVML超时等于GPU OOM”不成立。MemAvailable容量充足也
不保证分配及时完成。准确内核order与锁持有者仍未观测，不冒充
完整驱动内部因果证明；连续虚拟区间不等于整笔物理连续分配。

**没有确认可直接修复的应用所有权/泄漏缺陷。** 当前不能仅根据这些
证据把cudaMalloc替换为managed/分段映射，或延长NVML时限作为修复。
已验证的可靠运行合同仍是实验加载前定向回收模型payload至零、
64GiB memcg、原监控门；本轮否决把“最多16GiB前缀回收”提升为替代
合同。该处理牺牲跨启动热文件缓存，不影响服务内请求保留缓存。
全回收有效只证明该条件可用，不证明清空全部文件在理论上是必要条件。

本轮按有界调研收尾，**热缓存直接启动仍未修复/接受**。真实共享缓存
实现和性能采样仍暂缓；本轮收尾不自动恢复优化。后续若必须保留热启动，
应独立选择分配策略原型（需完整指针/生命周期合同与质量验证）或匹配
Thor驱动的修复验证，而不是继续搜索清理比例。无需替换默认或执行
部署健康检查，因为生产运行时与已接受工具均未修改。
