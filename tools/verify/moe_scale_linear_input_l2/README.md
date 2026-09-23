# 四分支 layer2 QKV/Z/A/B 输入投影

使用各支自身layer2 HC mixed，重读本层四组checkpoint投影权重，
复用冻结记录算法，原支四组输出完整恢复。按自身输入支持集核对
全部变化token，保留完整16份BF16输出、所有差异/行分布。

prepare.py从moe-scale-linear-input-l1-20260924清单验证通用gemm
二进制、源码与旧零警告构建日志，复制到新prepared目录；run.py
再次核对来源和副本摘要。不重新编译，不把旧构建日志称新构建。
运行run.py，stdout留prepared/run.log；真实终态后seal.py，stdout
也留prepared。目录固定moe-scale-linear-input-l2-20260924，新实验
换名，不覆盖任何冻结路径。

预算1.5GB+20GiB余量，封存69项1204310257字节。未保留GEMM
累加器，不是独立高精度整模型证明。终点原始投影，尚未layer2
短卷积/qk norm/GDN；无生产/观测修改，无新HTTP或性能结论。
