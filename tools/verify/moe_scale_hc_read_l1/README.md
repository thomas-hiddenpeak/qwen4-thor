# 四分支layer1 HC读取传播

复用moe-scale-ple-chain-20260924四支PLE输出与原冻结HTTP参数，
不改生产/观测、不新增HTTP。复制至prepared新目录并去掉.in、
调整路径，新建e2e/reference。stdout留prepared；真实终态后
seal.py核对28项branch×stage覆盖并生成终态清单。

norm.cu与上一PLE阶段相同，本次按封存摘要复用原零警告二进制，
reused-build.json记录来源。若重新构建，沿用moe_scale_ple_chain
README的nvcc命令。gemm.cpp用g++-14 C++23 -O2 -ffp-contract=off
-Wall -Wextra、CUDA include/lib64、cudart/cublasLt；mix.cpp同
CPU选项加-fopenmp。三个build.log必须为空后运行run.py。

四分支各自：HC norm→down投影→scaled SiLU→up投影→四路
sigmoid混合；另从新norm做inject投影→scaled sigmoid写回门值。
norm为CPU指定顺序+设备rsqrt，GEMM用原记录cuBLASLt算法；
SiLU/两类sigmoid使用绑定的完整BF16设备函数表，CPU四路顺序
FMA及除4后BF16。实际输入与门值不再接回原支结果。

原分支所有阶段恢复，重读4组checkpoint参数；每阶段差异支持
不能越出其自身输入的变化token。完整中间、mixed及新gate保留，
全部差异索引/行分布保存，norm还保留primitive与不同项未舍入值。
未保留GEMM累加器或匹配位置最终未舍入值。
本次moe-scale-hc-read-l1-20260924，约1.15GB，预算2.5GB+20GiB。
终点为layer1线性注意力输入与预备inject gate，注意力尚未执行。
