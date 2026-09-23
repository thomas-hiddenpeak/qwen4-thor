# 完整共享投影参考

复用moe-full-shared-20260924三侧HTTP冻结证据；生产和观测器均
未变，不重跑HTTP。将模板放入新的prepared目录，修改run.py的
输出路径；不可覆盖旧证据。本次目录moe-full-shared-reference-20260924。

reference.cpp以g++-14、C++23、-O2 -ffp-contract=off -Wall -Wextra
及CUDA include/lib64构建，链接cublasLt/cublas/cudart；生成reference
和空build.log，再运行run.py。其运行顺序为来源/权重直接核对、
96次实际算法重放与FP64 Dgemm、每次末行CPU顺序FP64交叉核对。

所有完整重放/BF16舍入参考通过实际基底加delta无损保存，临时完整
输出仅在逐字节/SHA重建一致后删除，末尾再完整重建一次。全部
FP64差异值及末行CPU/GPU FP64保留，一致位置非末行未舍入FP64
不落盘。参考预算600MB，保留20GiB空闲。CPU/GPU末行原始FP64
差异也必须报告，不能仅报告舍入后相同。

高精度参考仍使用实际上游输入，是条件算术参考，不是整模型前向。
原错题、尺度缺陷及因果关系未解决，不宣称精度或性能验收通过。
