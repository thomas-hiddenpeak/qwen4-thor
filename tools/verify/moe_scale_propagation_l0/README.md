# 首层输入尺度修正到GU的传播

复用已冻结原4K HTTP操作数，不改生产、不启动新HTTP。复制模板
至prepared新目录并更新路径，新建对应e2e/{quant,inputs,gu}。
quant.cpp用g++-14 C++23 -O2 -fopenmp -ffp-contract=off -Wall
-Wextra；gu.cpp相同编译选项（无需OpenMP），CUDA include/lib64
及cudart/cublasLt构建。两个build.log均为空后运行run.py，stdout
留prepared；真实终态后seal.py，禁止封存后追加归档日志。

保持首层实际HC mixed输入/路由、权重、alpha、算法和行映射。
CPU枚举独立E4M3 RNE尺度，使用冻结设备ratio，并核对CPU ratio
相同；重新编码对应E2M1 payload。断言改变尺度只有[248,432)
高区间，FP4字节变化只出现在尺度变化组。此前实际SF下FP4全同
的冻结参考用作基线。不是中间尺度修正，也不改本次其他层。

根据专家offset切分输入，按已观测reorder映射至GU行序。复用
记录cuBLASLt算法，保留全部完整输出和差异索引/逐行计数；核对
所有输入未变的行输出仍全同。读取首层所有活跃专家九段checkpoint
并核对摘要；没有用其他任务的未提交文件。依赖cuBLAS，不能当成
硬件独立矩阵证明。变化是局部因果，不是最终答案或正确性证明。

本次moe-scale-propagation-l0-20260924。预算1.5GB及20GiB余量，
实际约277MB。传播终点仅首层GU BF16，后续SwiGLU/中间量化/
down/合并尚待接通；不能将首层任务描述写成已经全部完成。
