# 四分支 layer1 NormGate / out projection

使用封存GDN的各支Y与各支新Z；原分支恢复完整NormGate输出及
out projection消费者。无生产或观测改动，无新HTTP。CPU平方树归约，
设备rsqrt/sigmoid，CPU三次FP32乘法及BF16舍入；out投影复用捕获的
cuBLASLt算法、alpha1/beta0、32MiB workspace，输入为各支新NormGate。

模板去.in复制到 `.q4t-work/prepared/moe-scale-linear-output-l1-20260924/`，
新建对应e2e/reference目录。norm用g++-14 C++20 -O3 -fopenmp
-Wall -Wextra -ffp-contract=off；nonlinear用nvcc C++20 -O2
-arch=sm_110a -ccbin=g++-14，host -Wall,-Wextra,-ffp-contract=off。
replay用g++-14 C++20 -O2 -Wall -Wextra，链接cublasLt/cudart。
空build.log后run.py，stdout留prepared；进程终态后seal.py，封存日志
也留prepared。新实验换目录，不覆盖。输入链接不修改。

预算4GB+20GiB余量；封存144项3631648388逻辑字节。保留完整NormGate
三种参考、参数/设备初等值/rawFP32及完整out BF16和全部差异/行分布。
CPU/FP64差异不隐藏；不是高精度整模型对照，没保存GEMM累加器。
终点注意力输出，HC写回在后续独立阶段。
