# Decode 缓存供给机制 — Goal 启动

更新：2026-10-06。承接39b879d，新分支
`codex/offload-supply-20261006`。先做有界离线机制研究，当前没有运行时
改动、新模型或HTTP。上一阶段的平票候选拒绝、旧性能NO_GO与默认关闭保持。

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
这些结论仍需成组host合同核对，不能泛化到大prefill内的singleton子块，
也不能把phase1并行/累计timer相加为独占SSD/H2D等待。

只有离线门槛无法排除直接解释时，才另冻一组默认关闭的窄观测。
最多1服务11题HTTP质量加4服务16请求的S/L×off/on对照，共5服务27HTTP；
观察臂选择规则已在计划中固定。当前没有启动这个条件范围。
任何提速实现与完整五档/目标/history/资源验收另行冻结，本研究不接受性能。

## 进度与保护

源码与调度合同、纯host上界工具与供给分析器已完成。上界22项、分析器
19项，共41项直接合同首批全过；输入提取闭合，实际16请求分析尚未执行。
两项来源绑定静审修正在首次测试前完成，原发现保留。
所有新产物位于`.q4t-work/offload-supply-20261006/`。
MAIN原七项修改/diff/binary入口身份已固定；模型仅记录228项元数据及
config/index摘要，不读写权重payload；reference只读。54GB仍未知。
