# 完整PLE key/value投影参考

复用ple-full-projection-v2-20260924已完成三侧HTTP的冻结证据，
生产/观测器均未改，不重跑HTTP。模板复制到prepared新目录并
去掉.in，修改输出路径，不覆盖旧证据。本次ple-full-projection-reference-20260924。

reference.cpp：g++-14 C++23 -O2 -ffp-contract=off -Wall -Wextra，
CUDA include/lib64，链接cublasLt/cublas/cudart，生成reference及空
build.log后运行run.py。分别执行完整实际算法replay和FP64 Dgemm，
FP64经FP32/BF16舍入；两组末行另用CPU顺序FP64 FMA交叉核对。

实际缩放后的lookup输入、权重、算法和输出逐份绑定，checkpoint
片段直接重读。每次只写一种完整BF16临时参考，以实际基底+全
索引/位值delta恢复，逐字节及SHA一致后删除临时文件，最后再
完整重建四份参考。保留所有FP64差异值、末行完整CPU/GPU FP64；
其他一致位置未舍入FP64不落盘。总预算100MB，另留20GiB。

这是给定实际输入后的条件算术参考，不是独立整模型前向，不
将高精度投影传播到后续PLE。原错答仍在，不作为质量/性能验收。
