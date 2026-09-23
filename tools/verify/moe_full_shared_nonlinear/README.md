# 完整共享SwiGLU与最终MoECombine参考

复用moe-full-shared-20260924三侧HTTP冻结证据，无生产/观测改动。
模板复制至prepared新目录并去掉.in，重跑修改run.py输出目录，
不覆盖冻结证据。本次目录moe-full-shared-nonlinear-20260924。

cpu.cpp：g++-14 C++23 -O2 -ffp-contract=off -fopenmp -Wall -Wextra，
输出cpu，构建日志build-cpu.log；device.cu：nvcc C++20 -O2
-arch=sm_110a -ccbin=g++-14 -Xcompiler=-Wall,-Wextra，输出device，
日志build-device.log。两日志须为空，然后运行run.py。

CPU逐线程十项fma、256线程加法树重建gate dot；设备仅计算sigmoid。
SwiGLU两次FP32乘法再BF16，最终合并FP32 fma再BF16。另保存纯
CPU expf版本，以及顺序FP64 dot/stable sigmoid/FP64运算后经
FP32/BF16舍入版本。参考都使用实际GU/down/routed/x，不传播高
精度投影或修正尺度，不是独立整模型前向。内部融合dot未直接观测。

全部三种BF16参考采用实际基底+索引/位值delta；每层完整临时
文件逐字节/SHA恢复一致后删除，最后再次完整重建审计。保存
CPU FP32/FP64 dot、设备sigmoid和BF16 sigmoid全表，全部差异
位置可恢复；非线性算术未舍入逐元素参考不落盘。600MB预算，
20GiB磁盘余量。原错答仍在，指定算术一致不等于模型质量通过。
