# 完整QSA主投影矩阵乘参考

复用qsa-full-main-20260924已完成三侧HTTP并封存的证据，不改
生产/观测器、不新增HTTP。复制模板至prepared新目录、去掉.in，
更新路径。本次qsa-full-main-reference-20260924。

reference.cpp以g++-14 C++23 -O2 -ffp-contract=off -Wall -Wextra，
CUDA include/lib64，链接cudart/cublas/cublasLt构建reference。
build.log须为空后运行run.py，stdout保存到prepared/run.log。

绑定projection-map及全部实际操作数/权重/算法，直接重读checkpoint
权重段。48个4096行GEMM分别按记录算法重放和FP64 Dgemm重算，
后者经FP32/BF16舍入。每组末行另用CPU顺序FP64 FMA检查原始位值
及舍入结果；旧末行FP64参考的BF16结果也全量对照，差异有则保留。

每组两种完整BF16参考以实际基底+全部索引/值delta保存；删除
临时副本前逐字节/SHA恢复，最终另恢复96份完整参考。保存所有
FP64不同项原始值，以及完整CPU/设备末行。匹配位置的非末行
未舍入FP64不保存。总预算1GB，另留20GiB。

本参考仍依赖cuBLAS且使用实际捕获输入，不是独立组合前向，
不能将条件重放相同当成整模型正确。run.py真实终态后执行seal.py，
stdout仍在prepared；复制终态日志再生成清单并全量复核。
