# 四分支 layer1 路由传播

冻结HTTP复用，使用各支新MLP HC混合输入，重新计算512专家分数、
前10专家及路由权重。checkpoint gate权重重读，记录cuBLASLt算法，
CPU按分数降序/专家ID升序选择；设备仅exp，CPU顺序10项求和、
求倒数和乘权重。原分支完整logits/IDs/weights必须全部恢复。
三修正支是layer0干预传播，本阶段不增加layer1尺度修正。

模板去.in复制到 `.q4t-work/prepared/moe-scale-router-l1-20260924/`，
新建对应e2e/reference。replay用g++-14 C++20 -O2 -Wall -Wextra，
链接cublasLt/cudart；exp用nvcc C++20 -O2 -arch=sm_110a
-ccbin=g++-14，host -Wall,-Wextra,-ffp-contract=off。空build.log
后run.py，stdout留prepared/run.log；真实终态后seal.py，stdout
也在prepared。不覆盖冻结文件；更换实验名称须同时修改封存路径。

预算200MB+20GiB余量，封存78项23315503字节。保存完整logits、
IDs、deltas、exp、权重、全部差异、顺序/集合变化行及逐行专家替换。
无GEMM累加器、高精度整模型或最终答案证明；无生产/观测改动及
新HTTP。后续必须按新IDs重新分组，不能复用旧专家行数和重排。
