# Decode 缓存供给机制 — 离线闭合，窄观测实施中

更新：2026-10-06。承接39b879d，新分支
`codex/offload-supply-20261006` 的离线工具8ea3d32已推送；观测实现位于
独立分支 `codex/offload-supply-observer-20261006`。尚无新模型或HTTP。
上一阶段的平票候选拒绝、旧性能NO_GO与默认关闭保持。

## 问题与范围

[冻结计划](evidence/offload-supply-20261006/scope-plan.json) SHA256
`2bf0aa00fceebdc00d5b1e04ced774f3eb2ba4104d58e056a21d7c68205086e4`。
复用原四个on服务16条请求，按实际decode入口分析255个forward与48层。
已有全链GPU回放精确证据复用；本次每个decode条件化于其实际入口，
不是新的反事实连续策略，也不猜测worker顺序。

具体假设：同一decode计划内，先完成的worker为GPU victim预留mirror
写回槽时，可能抢占另一个尚未claim的needed专家镜像，使其转为软件读。
抢占发生在reserve阶段，早于映射publish；GPU victim不在needed不能
保证被覆盖的mirror entry不在needed。该风险由源码证明可能，实际频次未知。

先提取原完整快照的逐层L2 clock：成功阶段的增量等于L2 hits+read misses；
全局L2hits为零且各层非负时，才能将逐层增量称为实际软件read misses。
与已精确GPU loads相减得到实际mirror hits，并要求聚合闭合。
随后按实际GPU缺失/驱逐流，计算同plan直接损失保守上界
`min(8, max(0,m−1), occupied victims, misses∩possible mirror IDs)`。
possible集合从实际decode入口mirror开始，只加入此前计划的GPU victims。
不把mirror假设成最近8次驱逐，不用上界差冒充实际损失差。

主端点为k1，同arm S/L各自比较；k0与k2/k3保留。若A/C两臂的长前驱
直接损失上界都小于实际mirror命中缺口，只排除“全部由当期直接竞争
解释”，不排除历史间接效应或反事实修复后的收益。

## 等待边界与条件后续

true T=1路径在计划前已同步单一模型stream；top10、L2=16及完整needed
保护限定L2备用选择。mirror供给只能来自计划入口，当前GPU victim不在
needed，因此不能把mirror source的lazy event标志当作未完成GPU拷贝。
这些是限定条件下的源码结论，不能泛化到大prefill内的singleton子块，
也不能把phase1并行/累计timer相加为独占SSD/H2D等待。

只有离线门槛无法排除直接解释时，才另冻一组默认关闭的窄观测。
最多1服务11题HTTP质量加4服务16请求的S/L×off/on对照，共5服务27HTTP；
观察臂选择规则已在计划中固定。离线结果触发A臂观测，仅准入实现。
任何提速实现与完整五档/目标/history/资源验收另行冻结，本研究不接受性能。

## 进度与保护

源码与调度合同、纯host上界工具与供给分析器已完成。上界22项、分析器
19项，共41项直接合同首批全过；原55项GPU合同按身份复用。
实际16请求分析与独立审查已通过：195840个decode层计划、768个逐层
供给分解、64窗口、16末态闭合；没有新增推理。
两项来源绑定静审修正在首次测试前完成，原发现保留。
所有新产物位于`.q4t-work/offload-supply-20261006/`。
MAIN原七项修改/diff/binary入口身份已固定；模型仅记录228项元数据及
config/index摘要，不读写权重payload；reference只读。54GB仍未知。

## 离线结果与条件判定

| 臂/位置 | mirror缺口 S−L | GPU loads L−S | read miss L−S | 长前驱直接损失上界 |
|---|---:|---:|---:|---:|
| A k1 | 470 | 227 | 697 | 1540 |
| C k1 | 452 | 129 | 581 | 1487 |
| A k2 | −272 | 308 | 36 | 1338 |
| C k2 | −215 | 239 | 24 | 1296 |
| A k3 | −96 | 114 | 18 | 1151 |
| C k3 | −93 | 102 | 9 | 1140 |

上界大于缺口，所以不能排除同plan直接竞争，**也不证明它实际发生**。
k2/k3的mirror方向反转完整保留。k1第32层缺口均为29，上界仅7/9，
该层全部差异不能由当期直接竞争解释；剩余不自动归因于历史竞争。
12个k1–k3探针没有mirror skips，写回=驱逐=加载；长前驱写回反而增加。
“写回不足”不能解释本批镜像命中减少。

[独立结果摘要](evidence/offload-supply-20261006/supply-independent-summary.json)
记录全部来源与范围；原结果SHA256为
`42948b8f2cad32b887efb4c399595bd0f86a58f2f8496a36c2acd5cc4462ca5e`。

## 已冻结的观测实施范围

[观测附录](evidence/offload-supply-20261006/observer-appendix.json)
固定默认关闭的 `Q4T_MOE_SUPPLY_OBSERVER`，只记录实际source、mirror
reserve/claim/publication关系和最多每层4个近期损失样本。不得改变
GPU选槽、worker派发、缓存策略/预算或CUDA同步；仅复用现有互斥区。
计划和持久状态各≤4096B，每请求新增JSON≤1MiB；关闭时省略观测字段。

先完成静审与必要构建；第一项新runtime测试为原固定质量11题（observer
on），其后成组host/protocol合同，再依次AS off/on、AL on/off各4请求。
四诊断服务均启用同一旧phase/cache/timing/route bundle，仅新observer
开关不同；A策略位0/0/0，16GiB host/cache、swap0、单流，MTP关闭。
总计5服务27HTTP，执行身份尚未准入。

观测会影响worker时序。正例只证明新instrumented请求中发生，零例不能
否定旧请求；单cell off/on差不能当纯观测开销或稳定速度因果份额。
不在本阶段实施优化，不用诊断替代五档性能验收。

实现首版已完成，四个运行时文件与23项C++、23项Python直接合同待执行。
独立静审修复无效环境值静默关闭、规划失败漏账两项问题；记录保留。
入口metadata在前一worker barrier后、下一dispatch前由caller复制，
不增加锁获取。所有跨worker事件仍在原有互斥区内。序列化额外记录
总字节与完整性，关闭时全部新JSON字段缺席。当前尚未构建或测试。
