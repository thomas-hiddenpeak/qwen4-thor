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

## 后续：完整4K NormGate与out_proj（2026-09-23）

生产二进制仍2392f6b1，计算路径未改。完整36层、4096位置的GDN
输出及z投影，已逐文件摘要接到本次NormGate输入；旧证据的摘要
清单核对通过。所有norm权重[128]与out_proj权重[2560,6144]同
checkpoint。norm输出与out_proj输入、out_proj输出与下游
CombineAndGroupedNormKernel的block_output均绑定实际指针及完整
字节，后者不是仅核对末行，也不是验证残差融合自身全行算术。

### 先行HTTP及启动超时

首轮linear-full-output-20260923在首组模型加载期间超过评估工具
固定180秒等待；没有发出HTTP请求，completed=0，服务被工具终止
退出−15。退出前日志仍在加载阶段，进程读取模型有进展；没有据此
判为模型计算错误。首轮exit/server/driver记录及failure-binding保留。

随后给tools/evalscope/run_acceptance.py添加--startup-timeout，默认
仍180秒，要求正整数并将实际值写入server-command.json。本次v2
显式600秒，新目录重新开始三侧HTTP。该工具改动后的首项测试就是
真实HTTP；没有前置单测或微测试。请求超时、HTTP质量判断、五档
性能标准及生产服务参数均未变，详见EVALUATION.md。

v2三侧都4096输入、7输出、600440、stop，服务0、质量驱动1；这是
观测一致性门禁，不是题目回答正确。72组norm/out元数据各完整，
36次完整下游消费绑定通过。首组v2实际启动约29.6秒：本次证明600
选项可用于真实HTTP并留档，没有实测“加载超过180秒仍成功”的
边界，也未证明首次慢加载的具体资源原因或加载性能已经改善。

### 完整数值核对

| 对象 | BF16输出总数 | 指定参考差异 | 其他参考差异 |
|---|---:|---:|---|
| NormGate | 905969664 | CPU归约/乘法＋设备rsqrt/sigmoid：0 | 纯CPU8214；FP6410615 |
| out_proj | 377487360 | 记录算法、完整实际矩阵重放：0 | FP641431743 |

NormGate参考独立在CPU对每个128维head做平方及64→1加法树归约，
加实际FP32 epsilon=1e-6；设备辅助只提供rsqrt和sigmoid，三次
FP32乘法及最后BF16舍入在CPU完成。纯CPU数学函数及FP64版本
也完整保存；不使用差异比例阈值通过，不宣称独立验证设备数学库。

out_proj全行重放保留实际算法、布局、FP32计算、alpha1/beta0及
32 MiB workspace，检查算法兼容性；这仍使用同一cuBLAS算法，不
等于独立证明库算术。另以实际BF16权重/输入精确扩展FP64，独立
Dgemm生成完整结果，再FP32/BF16舍入；全部1431743个差异索引
和完整FP64分数留存。末行差异索引与旧CPU FP64参考逐项一致，
仍为原356项，没有重新生成或替换历史比较对象。

辅助程序全部零警告构建，分析/核查退出0。全量核查重读参考输出、
差异索引、权重、上游绑定和下游消费者，并保留首次启动失败清单。
没有接受任何新的吞吐或精度结论，生产代码和性能参考均未修改。

### 当前范围与下一步

在实际HC混合输入的条件下，本原4K请求36个linear层的完整输入
投影→短卷积→q/k归一化→GDN递推→NormGate→out_proj已连接到
指定参考及实际下游消费者。它不是从模型embedding独立生成的
整模型前向：完整HC混合/残差、MoE与QSA的全token范围仍未覆盖，
既有末prefill/首decode证据不能放大为全部位置。原错题和已知E4M3
尺度编码问题仍在，不能据此排除所有计算错误或证明错误答案的原因。

下一步梳理完整HC混合/残差的全位置参考与采集容量，避免用末行
结果代替全序列依赖。完整证据在
`.q4t-work/e2e/linear-full-output-v2-20260923/`，失败首轮目录单独保留。
模板在tools/verify/linear_full_output/：构建观测器零警告→run.py HTTP
→必要离线工具构建→analyze.py→audit.py；新实验使用新目录，不
覆盖已有证据。长加载场景可以显式设置启动等待，不能重写旧失败。
