# 完整indexer查询归一化/旋转组合参考

复用qsa-full-indexer-20260924已冻结三侧HTTP输入，不新增观测或
HTTP。复制模板至prepared新目录，去掉.in并更新路径，新建对应
e2e/reference目录。reference.cu以nvcc C++20 -O2 -arch=sm_110a
-ccbin=g++-14及host -O3,-fopenmp,-Wall,-Wextra,-ffp-contract=off
构建reference，build.log必须为空。run.py stdout留prepared，
真实终态后seal.py，seal stdout亦留prepared，不追加封存日志。

12层4096查询，每query四个128维head。CPU四个32-lane XOR归约
树再顺序求和，norm后BF16舍入，再对前64维旋转。四种参考：
device仅rsqrt/pow/cos/sin依赖设备，CPU显式current*cos FMA；
cpu用double函数舍入FP32后相同顺序；fp64_bf16为双精度公式但
保留内部norm BF16边界；fp64为省略内部norm舍入的高精度公式。
后两者不是运行时精度合同，全部差异保留，不能自动判为实现错误。

每个变体传播自己的norm结果至RoPE，内部norm未观测，不能宣称
中间norm逐项对照实际。实际IQ投影仍为输入，不是整模型独立传播。
完整48份最终BF16参考用基底+delta恢复；所有不同项最终未舍入
值、完整舍入norm、方差/倒数/数学表保留。匹配最终原值及完整
FP64未舍入norm不保存。后64维不旋转，单独报告差异。

本次qsa-full-indexer-query-20260924，约344MB，预算1GB，
另留20GiB。检查完整layer×variant覆盖、参考重建及全部摘要。
