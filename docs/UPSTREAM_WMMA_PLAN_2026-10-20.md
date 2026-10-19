# 独立WMMA参考预算（2026-10-20）

已取得实际SM110 cubin；两内核指令前缀317条相同，覆盖全部8条静态
HMMA.16816.F32.BF16指令与累加主循环。输出阶段后才出现分歧。

独立CUDA源不引用q4t/cuBLAS实现；使用CUDA WMMA 16×16×16 BF16
操作，K=2560以16顺序递增，FP32 accumulator。3个warp block覆盖48列；
X四行补零至16行。输出FP32由host精确nearest-even转BF16。
一次实际夹具与两个控制（全零、K=0/15/16/2559的稀疏基向量），
共3次launch，无模型/cuBLAS/HTTP。基向量各head输出head+1，补零行
必须为零；真实夹具192个FP32和BF16结果分别与已有cuBLAS记录比较。

编译命令：nvcc -std=c++23 -ccbin g++-14 -arch=sm_110a -O3
-Xcompiler=-Wall,-Wextra,-Werror，零警告。产物在
.q4t-work/upstream-wmma-20261020。run_wmma.py全程经gpu_exclusive.py
lease-v1持锁，输出run-v1；记录源码、binary、原始输出、输入摘要和终态。
失败停止、保留、不自动重试；GPU竞争/监控失败ENV_ABORT。
参考只独立于q4t/cuBLAS软件，使用同一GPU tensor指令，不宣称独立
硬件实现或反推出未公开的tensor内部加法树。
