# 四分支 layer1 新路由 down 投影

使用自身中间FP4与新专家行数，checkpoint down权重和尺度重读。
每次检查记录算法对新shape/workspace兼容，不做算法回退；输入
已经是新顺序，不套旧reorder。输出按(token,expert)对齐实际down。
原支104857600个BF16全部恢复，所有新shape兼容，共有中间输入
未变的行输出也未变。新增147/212/238行输出单独保留。

模板去.in复制到 `.q4t-work/prepared/moe-scale-down-l1-20260924/`，
新建对应e2e/reference。down用g++-14 C++20 -O2 -Wall -Wextra，
链接cublasLt/cudart。空build.log后run.py，stdout在prepared/run.log；
真实终态后seal.py，stdout也留prepared。输入只读硬链接，新实验
换目录，不修改冻结文件。

预算3GB+20GiB余量，封存10480项2128867166逻辑字节。完整BF16
输出、全部共有差异/行分布、旧down行映射、新flat slot及权重计划
保存，未保存FP32累加器。本次只传播layer0干预，不修正layer1。
记录算法重放不等于独立GEMM或最终任务证明，无生产修改/新HTTP。
