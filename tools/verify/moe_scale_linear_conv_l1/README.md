# 四分支layer1短卷积与历史传播

复用moe-scale-linear-input-l1-20260924新QKV及原冻结HTTP权重/
零历史，不新增观测或HTTP。复制至prepared新目录，去掉.in改
路径，建立e2e/reference。conv.cu用nvcc C++20 -O2 -arch=sm_110a
-ccbin=g++-14，host -O3,-fopenmp,-Wall,-Wextra,-ffp-contract=off。
build.log为空后run.py，stdout留prepared；真实终态后seal.py。

CPU按tap0..3顺序FMA实现K4/dilation1卷积，设备仅SiLU，CPU
BF16舍入。各支使用自己QKV；原支输出同已捕获GDN输入，最后3行
原始QKV转[C,3]的历史同原更新状态。变化支持集为实际QKV变化
位置向后0/1/2/3移位，不得越出。完整FP32累加/SiLU/BF16及
历史、全部差异/行分布保留并核对舍入、有限性。

本次moe-scale-linear-conv-l1-20260924，约1.88GB，预算3GB+
20GiB余量。终点GDN原始QKV，未做q/k norm、A/B变换或递推。
历史相同不代表GDN或整模型decode状态相同；生产未改。
