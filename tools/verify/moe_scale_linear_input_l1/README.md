# 四分支layer1线性注意力输入投影

复用moe-scale-hc-read-l1-20260924四支mixed输入及冻结原4K HTTP
权重/算法。复制至prepared新目录并去掉.in、改路径，新建
对应e2e/reference。gemm.cpp以g++-14 C++23 -O2 -ffp-contract=off
-Wall -Wextra、CUDA include/lib64、cudart/cublasLt构建；build.log
为空后run.py。stdout留prepared，真实终态后seal.py。

读取4组checkpoint权重，按记录算法重算全部4096行QKV/Z/A/B，
宽度10240/6144/48/48，输入宽2560。原四组输入必须同HC baseline，
原输出必须逐位恢复。修正输出不得越出各支mixed变化token。
完整16份BF16输出及全部差异/行分布留存，不保留GEMM累加器。
这是记录算法传播，不是硬件独立或FP64整链证明。

本次moe-scale-linear-input-l1-20260924，约721MB，预算1.5GB及
20GiB余量。终点投影原值，短卷积与GDN另行接通；生产未改。
