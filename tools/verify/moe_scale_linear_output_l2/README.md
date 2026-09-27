# 四分支 layer2 NormGate / 输出投影

prepare.py校验并复用已封存norm二进制/源码；nonlinear和replay
增加层号参数后零警告编译，原算术不变。旧日志单列reused-build。
随后执行新prepared/run.py，stdout写prepared/run.log；真实终态
后运行seal.py，stdout仍留prepared。不要覆盖冻结目录。

输入为各支自身layer2 GDN Y及Z投影；NormGate采用CPU指定树形
平方归约、设备rsqrt/sigmoid、连续三次F32乘法/BF16；out使用
本层checkpoint和记录算法。原支全量恢复NormGate、out及捕获
HC消费者。CPU/FP64 norm额外差异保留，不是独立高精度整模型。

保留完整BF16、全部差异、数学F32及token分布。新生成的独立F32
和差异索引在最后消费后gzip level1压缩；逐份流式解压确认原SHA
和字节数，写compression.json映射后才移除新原始副本。BF16
端点保持原格式，已有证据不修改。预算4GB及20GiB余量。

仅复用已门控HTTP，生产未改，不新增HTTP或性能结论。终点在
HC残差前；不得据局部恢复宣布最终任务正确。结果见尺度传播报告。
