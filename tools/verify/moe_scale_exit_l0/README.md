# 首层输入尺度修正的down与MoE出口传播

复用moe-scale-inter-l0-20260924冻结量化结果和原三侧HTTP数据，
无生产修改/新HTTP。复制至prepared新目录、去掉.in、更新路径，
创建e2e/{inputs,down,combined}。down.cpp用g++-14 C++23 -O2
-ffp-contract=off -Wall -Wextra，CUDA include/lib64以及
cudart/cublasLt；combine/final加-fopenmp，无CUDA依赖。三个
build.log为空后run.py，stdout留prepared；终态后seal.py。

先从原实际down重算十路加权合并与shared最终相加，逐位恢复后
才传播变化。新中间FP4按实际reorder映射到down行序，核对flat
身份；重读所有首层down checkpoint四段。记录算法重放新down，
CPU十槽顺序FMA合并，加未变实际shared down及冻结gate sigmoid，
一次FMA后BF16舍入。shared输入、路由保持原实际值。

完整新down、两分支routed F32/final舍入前F32及BF16、全部差异
与token分布保存；改变必须限于输入变化行/token。不是独立硬件
算术证明。传播终点是HC write读取的MLP block，尚非残差trunk，
没有传播到layer1，也不证明最终答案的变化或质量改善。
本次moe-scale-exit-l0-20260924，约482MB，预算1.5GB+20GiB。
