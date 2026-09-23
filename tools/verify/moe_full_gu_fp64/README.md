# 完整 4K MoE gate/up 高精度参考

复用 `moe-full-gu-20260924` 三侧 HTTP 通过观测不干扰检查的冻结证据。
原请求仍答错；此处没有生产或观测器改动，不重复 HTTP、不运行 bench。

模板复制到 `.q4t-work/prepared/moe-full-gu-fp64-20260924/`，去掉
`.in` 后构建 `fp64.cpp`：C++23、`-O2 -ffp-contract=off -Wall -Wextra`，
CUDA include/lib64，链接 cudart/cublas，生成 `fp64` 与 `build.log`。
构建日志必须为空，然后依次运行 `run.py`、`audit.py`。同名证据目录
不可覆盖；重跑要整体使用新目录。

独立解码 packed FP4 及逻辑 E4M3 SF，不调用生产转换函数。实际
checkpoint、行序和量化输入逐项绑定；alpha 保留现场的 FP32 乘法
边界。全部输出用 FP64 Dgemm，再经 FP32/BF16 最近偶数舍入；最后
一个原输入 token 的480个专家行还用 CPU 顺序 FP64 乘加交叉核对。
CPU 不复用 cuBLAS 的累加顺序，关闭乘加收缩和 fast-math。

保存完整舍入 BF16、全部差异索引及对应未舍入 FP64；末 token 保存
完整 CPU/GPU FP64，其他一致位置的未舍入 FP64 不落盘。容量预算
8GiB参考、20GiB余量，写入时再次检查余量，不超额写满磁盘。

这项参考保留实际 SF，未传播修正后的编码；不能抹去前序量化错误，
也不覆盖 SwiGLU/中间量化、down/shared/combine 或整模型错答因果。
