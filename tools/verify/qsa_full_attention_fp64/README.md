# 完整QSA高精度及显式舍入参考

复用qsa-full-selection-20260924三侧HTTP冻结输入，生产和观测器不变，
不新增HTTP。模板复制至prepared新目录、去掉.in并更新输出路径；
不能覆盖旧证据。本次前缀qsa-full-attention-fp64-20260924。

reference.cu构建为reference：nvcc C++20 -O2 -arch=sm_110a，
-ccbin=g++-14，host -O3,-fopenmp,-Wall,-Wextra,-ffp-contract=off。
build.log必须为空。运行run.py，真实终态完成后再运行audit.py，随后运行finalize.py加实验前缀；
不因等待超时重复启动。每块32 query，1536块，OMP_NUM_THREADS=8。
容量预算4GB、另留20GiB；实际参考先生成后按无损差异存储。

三个参考变体均以捕获的Q/K/V、gate、选择列表、页表为输入：

1. FP64 QK逐维FMA、全选中集合稳定softmax、FP64 PV逐位置FMA、
   FP64 sigmoid和门控，最后经FP32/BF16舍入。
2. 在上述参考的门控前额外加入BF16物化。
3. 按16位置块在线softmax，概率经FP32/BF16舍入，块内FP64 PV及
   块间FP64 FMA；分母仍累加未量化概率，门控前亦物化BF16。

使用独立CUDA FP64核，不调用生产kernel或Tensor Core；仍依赖
设备双精度数学函数。每层末query另用CPU std::fma/std::exp重算，
记录全部QK和最终未舍入/舍入差异。不能将末query CPU对照扩展
为全部query的纯CPU参考，或据高精度差异直接判定实现错误。

保存所有最终差异的索引、BF16值、FP64值与所有query差异计数；
完整参考先逐块无损恢复再删临时文件，最后再恢复12层×3变体。
全query两种FP64分母、CPU/设备末query完整输出和评分均保留。
一致位置的非末query未舍入值和完整概率/QK/PV不保留。

audit.py绑定原清单及已核对的全选择参考，审计全部差异文件、
覆盖和逐query计数，汇总全选/稀疏分支的分布。审计使用单独子目录
和清单，不修改初始冻结文件。没有独立组合前向、评分/投影来源
或错答因果结论；原错误及尺度缺陷不能被条件参考覆盖。

本次初始清单在最后一行stdout写入前生成，只有run.log末态摘要
变化。finalize.py逐项复核初始清单，旧日志前缀须同原摘要，追加
行须等于summary.json；其他差异立即失败。原文件和清单不覆盖，
最终以final-artifact-binding.json绑定所有终态文件并再次核对。
finalize的stdout不能重定向到即将封存的证据目录内。

归档目录保留本次实际执行的原工具；此处run.py模板已将最终打印
移至初始清单之前，finalize模板同时接受零差异及上述可证明的旧
日志追加情况。只调整归档顺序，不改变本次任何数值结果。上一轮
qsa-full-attention-primitives-20260924也已追加终态清单，原数据未改。
