# 完整PLE归一化与门值参考

复用ple-full-gate-norm-20260924三侧HTTP冻结证据，无生产或
观测器修改，不重跑HTTP。模板复制至prepared新目录并去掉.in，
更新run.py输出目录，不覆盖冻结证据。本次ple-full-norm-reference-20260924。

reference.cu：nvcc C++20 -O2 -arch=sm_110a -ccbin=g++-14
-Xcompiler=-Wall,-Wextra,-ffp-contract=off，生成reference和空
build.log，再运行run.py。每块128行，总128块覆盖三个norm和gate。

norm由CPU按每lane八值平方FMA、十组相加及全部32lane XOR
归约重建，设备只执行rsqrt。gate由CPU顺序2560次FMA及实际
scale生成raw，设备仅执行sqrt/符号/exp/sigmoid。纯CPU参考使用
double sqrt/exp转换FP32边界；FP64参考保留实际eps/scale，重做
平方和/点积及数学函数，输出经FP32/BF16舍入。使用实际上游
输入，不传播高精度norm输出到gate，不是独立整层/整模型前向。

每块三种完整BF16输出生成delta，逐字节和SHA恢复一致后删除
临时副本，最后重建12个完整kind/variant并检查连续覆盖。
保存所有FP32/FP64数学函数参数和设备结果，所有FP64最终差异
处未舍入值；一致位置的未舍入FP64输出不落盘。100MB总预算、
单块20MB临时空间、20GiB磁盘余量。内部实际归约值未直接观测。
