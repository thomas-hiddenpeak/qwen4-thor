# PLE embedding转换、缩放与投影核对（2026-09-23）

## 结论与边界

接受运行时未改，原4K质量题仍600440而非360284。prefill末10行
4086–4095及首decode4096的FP8转换、checkpoint缩放、两组投影
与既有门控/归一化链路已连接局部参考。尚未确认这些FP8字节来自
正确的hash/SSD行；不把本轮局部一致当作整模型正确或原错修复。

28160个FP8→BF16值以及28160个缩放输出逐字节同独立参考；
prefill记录算法重放128000个投影输出、decode CPU参考12800个
投影输出全部逐字节相同。实际两组权重及缩放系数匹配checkpoint。

## 首轮遗漏与观测门禁

首版参考假设FP8转换后的BF16缓冲内容直接供投影读取。首轮
观测构建零警告后先完成三侧原4K HTTP，均原错600440、4096
输入/7输出、stop、服务0、质量驱动1，实际指针也相同；但离线
输入字节比较失败，尚未执行投影参考。

随后读代码确认model.cu在Gather后调用ScaleBf16Kernel，按
ple_weight_scale原地相乘并再舍入BF16。因此同一指针跨越了
一次内容变换，首轮差异是参考合同遗漏，不能判作运行时错误。
首轮源码、HTTP、失败断言、analysis-failure.json及哈希清单留档
于`.q4t-work/e2e/ple-projection-reference-20260923/`。
参考decode工具另有一次字符串拼接编译错误，失败源码/日志保留，
修正为std::string后零警告；没有用失败构建执行参考。

v2新增实际缩放核系数和前后BF16捕获，要求缩放完成后才识别
投影。新观测零警告后重新完整三侧HTTP，仍相同原错、服务0；
2次转换、2次缩放、4次投影完整，此前门控阶段68份记录和首轮
92份记录逐字节不变，随后才开展数值参考。

## 实际输入与权重

转换捕获来自Fp8ToBf16Kernel实际输入与输出；缩放核输入指针
直接绑定转换输出，缩放前字节亦相同。实际系数为
0.00019931793212890625，逐位同checkpoint中
layers.1.ple.ple_embedding.ngram_embedding.weight_scale BF16[1]
展开的FP32。独立计算原BF16×实际系数并RNE BF16，28160输出全同。

FP8参考按E4M3FN位字段独立解释normal/subnormal/sign，转换无需
额外比例。prefill观察到209种编码、最大绝对值192；decode150种、
最大144；两组均没有指数15或NaN编码。因此只证明实际观察值，
不能把本样本外推为全部256种编码或既知其他量化器问题已修复。

缩放后结果与key/value投影共同输入逐字节一致。key权重
[10240,2560]、value权重[2560,2560]均同checkpoint中的对应
layers.1.ple.key_proj/value_proj.weight；实际输出指针绑定key
归一化输入和GatedValue的value参数，全部末10/首1行字节相同。
这接通了已检查的PLE后续归一化、门控及卷积所需输入。

## 投影算术与后续

FP64点积经FP32→BF16：prefill key有142项、value有33项不同；
decode key/value各0项，全部数值与差异索引保留。

Decode CPU按双fma链、八元素分组、warp shuffle-down归约，
两次12800个结果全部同实际输出。Prefill记录实际算法与BF16
布局、FP32compute/scale、A转置/B不转置、alpha1/beta0、默认
epilogue和32MiB workspace，独立进程cuBLAS130501 AlgoCheck后
以原M4096、N10240/2560、K2560重放。将捕获十行循环复制并对齐
末十行4086–4095，只核对这些行，128000结果全部同HTTP。
这复现原175项FP64差异，不独立证明cuBLAS内部算术，也不是
原始4096行完整输入的重放。

最终证据目录`.q4t-work/e2e/ple-projection-reference-v2-20260923/`，
含conversion-reference.json、projection-reference.json、
decode/prefill-reference.json、previous-binding.json、v1-binding.json、
artifact-binding.json及所有原始操作数/参考输出。最终工具零警告，
参考与审计退出0；没有运行时变更或新的性能验收结论。
下一步连接实际token上下文、checkpoint hash参数、SSD行内容与
本轮观察的原始FP8字节；全prefill GDN及最终头部等仍未确认。


## 2026-09-24后续：完整4K投影与查表交接

首版控制脚本在启动服务/发送HTTP前因来源manifest覆盖fixture
变量而KeyError，记录保留于ple-full-projection-20260924；不是
一次HTTP失败。v2仅修正控制变量，使用相同零警告观测器二进制。
首项实际测试仍为tools/evalscope关闭/开启/关闭三侧HTTP，均
600440、4096/7 token、stop、服务0、质量驱动1。六份元数据
通过且采集均在监听后，原错答保持不代表质量验收通过。

完整10485760个embedding值的实际FP8/转换BF16/缩放前后及
两次投影输入均全字节匹配冻结lookup step0。41943040个key、
10485760个value输出及下游实际消费者分别全字节匹配冻结
norm0 input和gated value，现场指针也接通。输入/输出不重复保存。

两组实际BF16权重65536000字节直接匹配checkpoint，加2字节
weight_scale共65536002字节；实际FP32系数为
0.00019931793212890625，与checkpoint和lookup记录相同。
保存两份实际算法，现场检查M4096/K2560/N10240或2560、BF16
布局、FP32计算/scale、alpha1/beta0、默认epilogue和32MiB
workspace。投影算术尚未重算，不能以这些边界代替计算正确。

证据：`.q4t-work/e2e/ple-full-projection-v2-20260924/`；模板：
`tools/verify/ple_full_projection/`。含来源绑定、checkpoint偏移/
摘要、完整HTTP与manifest。归档后可用21593632768字节，保留
20GiB余量。生产未改，下一步100MB参考预算内顺序执行完整
key/value记录算法与FP64参考，不同时保留多个完整临时输出。


## 2026-09-24后续：完整key/value投影算术

复用冻结HTTP，生产与观测器不改；参考零警告构建后，绑定完整
输入/权重/算法/输出并直接重读checkpoint片段。两个完整投影共
52428800个BF16输出，与本次实际记录算法重放完全一致。

FP64 Dgemm经FP32/BF16舍入后，key有52128项、value有12205项
差异，合计64333项，全部索引与未舍入值保留。末十行差异计数
分别142/33，与此前175项局部计数相符；计数相符本身不替代逐
元素比较。两个末行共12800个CPU顺序FP64与Dgemm原始位值
全部一致，舍入也一致。高精度差异不自动构成实现缺陷或错答归因。

完整重放与完整舍入参考均以实际基底+delta保存。每次只生成
一份完整临时BF16，逐字节/SHA恢复一致后删除，最后再次完整
重建四份参考；保留全部FP64差异及完整CPU/GPU末行，其他一致
位置的未舍入FP64不落盘。100MB预算内完成，生产未改。

证据：`.q4t-work/e2e/ple-full-projection-reference-20260924/`；
模板：`tools/verify/ple_full_projection_reference/`。归档后可用
21591506944字节，仍保留20GiB。原4K PLE的逐算子条件参考现已
覆盖完整prefill，但没有独立组合前向或传播高精度差异；原错题
仍在。下一步补完整QSA各query，先核对采集范围与磁盘容量。
