# 完整QSA注意力指定算术参考

复用qsa-full-selection-20260924三侧HTTP已绑定的冻结输入，不改变
生产或观测器，不新增HTTP。模板复制至新的prepared目录、去掉.in，
更新输出路径；禁止覆盖旧证据。本次qsa-full-attention-primitives-20260924。

reference.cu构建为reference：CUDA nvcc C++20 -O2 -arch=sm_110a，
-ccbin=g++-14，host -O3,-fopenmp,-Wall,-Wextra,-ffp-contract=off。
build.log必须为空，然后运行run.py。每块512 query，共96块，
OMP_NUM_THREADS=8。绑定84份来源及原始观测不变性记录。

独立设备kernel执行BF16 MMA QK/PV、在线softmax及sigmoid；CPU
按16位置块顺序FMA累加、除法、BF16舍入和门控乘法。不调用生产
模型kernel，但共用设备指令/数学函数，不是完全独立的硬件参考。

保留所有门控前BF16、FP32分母、设备gate查表及逐query差异数。
最终完整BF16参考以实际基底+全部差异索引/值无损保存，每块删除
临时输出前逐字节/SHA恢复，末尾再重建全部12层。最终不同项的
CPU累加、归一化和门控后FP32值全部保存；一致位置未舍入值及
QK/prob/PV/alpha完整中间量不落盘，不能声称这些内部量已逐项
对照生产。预算1GB，另保留20GiB。原错答及上游尺度缺陷不因
条件参考相同而消除；未完成独立组合前向或任务质量验收。
