# Linear门控与输出投影参考（2026-09-23）

## 结论与边界

原4K请求36个linear层，prefill末token与首个pooled decode共72组。
实际norm/out_proj权重全部逐字节同checkpoint；z投影→门控、
decode GDN读出→门控、门控→out_proj三个交接全部位同。
新旧1368份已有BF16快照逐字节相同。生产源码及接受二进制未改。

| 对FP64经FP32→BF16舍入的差异 | prefill | decode |
|---|---:|---:|
| RMSNorm × sigmoid(z)，每阶段221184输出 | 2 | 2 |
| out_proj，每阶段92160输出 | 356 | 22 |

输出投影decode由CPU双累加链/warp树重算，36次92160个BF16全部
位同；prefill记录算法同形状重放也全部位同。原378项高精度差异
保留，不能把cuBLAS重放等同独立内部算术证明。

门控CPU按FP32归约树、逐次乘法及CPU非线性参考重算，有5项不同。
设备只重建倒平方根和sigmoid，CPU完成归约及后续乘法，两者同时
替换后442368个BF16全部位同；只替换其中一种，各剩3项。
这解释了此样本中指定精度/近似下的局部输出，不是完整模型正确证明。
原错600440（应360284）仍未解决。

## HTTP门禁与实际操作数

新观测零警告构建后第一项执行tools/evalscope原4K无/有/无观测
HTTP。三次全文600440、4096输入/7输出、stop；服务均0，质量驱动
均1。只通过观测不改变本请求的检查，没有将错答标记成质量通过。
288份输入投影、72份门控、72份输出投影完整成功，才开始数值分析。
MTP关闭、接受二进制2392f6b1不变，诊断耗时不作为性能数据。

NormGateKernel启动前后记录每阶段对应末行，保存实际y、z、norm
权重和eps。每次norm之后用输出输入指针绑定out_proj，要求N2560、
K6144和M4096/1，不单靠投影形状猜测归属。prefill另核对cuBLAS
compute/scale、epilogue、布局、转置、alpha/beta，保存每次算法。
36层norm形状[128]、out_proj形状[2560,6144]均同checkpoint。
配置output_gate_type为sigmoid，rms_norm_eps为1e-6，与实际相符。

prefill门控输入为现场GDN输出，尚非独立生成的完整prefill参考。
decode输入另与此前捕获的GDN输出逐位绑定。两个阶段的z均同实际
in_proj_z结果，out_proj输入均同norm输出，避免使用重算值替代实际
输入而掩盖边界差异。

## 数值方法与证据

FP64参考直接解码捕获的BF16值；门控按每head128维均方、eps、
倒平方根、权重、sigmoid计算；输出投影为实际输入与实际权重矩阵
乘。全部差异索引及参考数组保存，不设容忍阈值。

门控指定算术参考先FP32平方，再64/32/16/8/4/2/1树形归约，
FP32均值与eps、逐次乘法。纯CPU非线性为NumPy FP64 exp和倒
平方根舍入到FP32、FP32 sigmoid除法；设备版本仅独立运行rsqrtf
和1/(1+__expf(-z))，不调用生产NormGate或GDN。三种替换结果均保存。
不能称为完全独立于CUDA数学库的逐位证明。

prefill输出投影重放使用记录算法、32MiB workspace、相同M/N/K与
布局，AlgoCheck验证后运行cuBLAS130501。末行复制成4096行，只
比较末行；不是原始全行输入。decode CPU独立执行实际GEMV的
32 lane双累加链、fma与16/8/4/2/1树相加，关闭隐式浮点收缩。
两阶段各36次输出逐字节复核。所有辅助工具构建零警告、执行退出0。

证据目录：`.q4t-work/e2e/linear-output-reference-20260923/`。
raw-http-audit.json、previous-binding.json、output-reference.json、
norm-order-reference.json、norm-nonlinear-reference.json、
prefill-replay.json、decode-replay.json及artifact-binding.json保留
实际操作数、差异、运行记录与工具校验值。仓库保存相应观测与分析模板。

## 下一步

线性attention输出端局部边界已接通。接下来核对hyperconnection
GRRead/Write实际混合、残差保存及层间交接；完整prefill GDN状态
生成、QSA缓存/索引器、PLE和最终输出头仍需独立覆盖。已知量化尺度
缺陷和原4K错答继续作为未解决项，不因局部位同或提交推送而验收。
