# 完整indexer矩阵参考

复用qsa-full-indexer-20260924三侧HTTP冻结输入，不新增HTTP。
复制模板至prepared新目录并改路径，reference.cpp以g++-14 C++23
-O2 -ffp-contract=off -Wall -Wextra、CUDA include/lib64、
cudart/cublas/cublasLt构建，build.log为空后运行run.py。
reference子目录预先新建；run stdout放prepared，终态后seal.py。

24组IQ/IK投影4096行，12组评分16384行，合计434110464输出。
记录cuBLASLt算法重放与FP64 Dgemm，经FP32/BF16舍入；每矩阵
最后一行CPU顺序FP64 FMA。评分最后一行仅末query的head3，
不能称全部四头CPU核对。使用实际输入，不传播高精度投影。
全部72份BF16参考基底+delta无损重建；所有FP64差异原值、
CPU/设备末行保留，匹配的非末行原始FP64不保存。

本次qsa-full-indexer-reference-20260924，预算1GB及20GiB余量。
score差异的因果可见性另由qsa_full_indexer_audit核对；重放相同
不代表独立cuBLAS证明或整模型正确。
