# 四分支 layer2 HC attention 读取

从自身layer1最终主干计算HC norm/down/SiLU/up/inject/gate/mix，
重绑layer2权重、捕获算法与实际边界。复用已封存、零警告的通用
helper，不为同一算术复制新的C++/CUDA源码或伪造新构建记录。

运行prepare.py：从moe-scale-hc-mlp-read-l1-20260924冻结清单核对
三个二进制、源码和旧构建日志，复制至新prepared目录并保存摘要。
run.py再次核对来源及副本，验证新层四组checkpoint，再运行参考。
stdout留prepared/run.log；真实终态后seal.py，stdout也在prepared。
prepare输出目录固定为moe-scale-hc-read-l2-20260924，新实验换名。

原支七阶段完整恢复；三修正支mixed变化9560513/9703396/9757998
个BF16，gate变化2871/3408/3579。自身输入支持集未越出，全部
输出、差异及norm数学证据保留。封存164项2404617660字节，
预算2.5GB+20GiB余量。终点layer2混合输入及残差门值，无新生产
或观测修改、无新HTTP；设备数学与cuBLAS仍为参考依赖。
