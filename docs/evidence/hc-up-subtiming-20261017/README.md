# 原二进制细分事件观测：执行前冻结

固定A B B A四条原200K off，单阶段GPU lease，原输入输出和二进制。
只启用Q4T_LAYER_TIMING与Q4T_LAYER_SUBTIMING，不叠加preload/PMU/Nsight。
已检查相关历史日志，没有可复用的200K subtiming结果。

源码覆盖：LayerSubTiming顶层六项分别PLE计算、HC read、attention、HC
write/read、MoE、末次write；嵌套detail中full_qkv/full_core/full_oproj
仅针对full attention。full_core包含QSA indexer与sparse attention等，
不能视为单个kernel。HC/MoE细节是所有层合计，不能声称只属于full层。
PLE top slot位于Gather之后，不能替代此前CPU读取/整理观测。

所有25条保留，比较chunk2–25。每类报告必须恰为24×8192+8181、字段
完整、非负有限、无同步失败；顶层六项与其total一致，嵌套不重复相加。
每项两对B−A必须均超过max(同身份R,1ms)才称可辨识正差；1ms覆盖打印
舍入累积，不是性能门槛或显著性检验。额外events/同步改变调度，插桩
TTFT不能验收，不将区间差当作单kernel净时间。错误停止，不自动重试；
负结果停止此测量类别，不加样本，不修改数值或prefill算法。
