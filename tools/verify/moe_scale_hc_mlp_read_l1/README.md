# 四分支 layer1 MLP HC 读取

冻结HTTP复用，以各支attention写回主干为输入；CPU指定归一化与
设备rsqrt、记录算法down/up/inject投影、完整BF16非线性表、CPU
四路FMA混合，保存新的MoE输入和MLP注入门值。原支七阶段全部
恢复；不使用原主干替换变化支。四组checkpoint重读绑定。

模板去.in复制到 `.q4t-work/prepared/moe-scale-hc-mlp-read-l1-20260924/`，
新建对应e2e/reference。norm用nvcc C++20 -O2 -arch=sm_110a
-ccbin=g++-14，host -O3,-fopenmp,-Wall,-Wextra,-ffp-contract=off；
gemm用g++-14 C++20 -O2 -Wall -Wextra，链接cublasLt/cudart；
mix用g++-14 C++20 -O3 -fopenmp -Wall -Wextra -ffp-contract=off。
三个空build日志后run.py，stdout留prepared/run.log；真实终态后
seal.py，stdout也在prepared。新实验新目录，不覆盖冻结文件。

预算2.5GB+20GiB余量，封存163项1671235764逻辑字节。保存完整
28份BF16输出、所有差异/token分布、norm数学参数及差异处舍入前值。
参考含设备初等函数与cuBLAS，不能视为独立高精度整模型证明。
终点MoE输入/MLP门值，路由和专家在后续阶段。生产未改，无新HTTP。
