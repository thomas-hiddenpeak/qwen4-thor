# QSA索引分数与压缩key交接（2026-09-23）

## 当前结论

原4K请求12个全注意力层，prefill末token/首pooled decode。
6292992个实际原始index key写入BF16同缓存，6291456个历史raw值
和1572864个已完成组的compressed key在首decode保持不变。
实际key归一化权重全部同checkpoint，实际query/key/score边界一致。

prefill由实际BF16点积矩阵重算ReLU/求和，加设备rsqrt缩放，
24576个FP32分数含mask全部逐位一致；decode由实际query和compressed
key执行CPU顺序fma/ReLU/求和/缩放，24576项也全部逐位一致。

尚未解释：prefill点积对FP64舍入BF16有4项不同；prefill压缩key
1572864输出对FP64均值/归一化/RoPE参考有1680项不同。
不因score汇总位同就宣布indexer完整正确。生产未改，原错仍在。

## HTTP与数据身份

新观测沿用已验证的12层selector/KV/attention记录，补充实际
query规范化后结果、raw key写入源与缓存、压缩key、norm权重、
三轴rope表及theta/epsilon、prefill点积矩阵与reduce输出、decode
score核实际query/ck输入与输出。仅seq_id0、prefill4096行/首decode。

零警告构建后首项三侧tools/evalscope原4K HTTP，均600440（应
360284）、4096输入/7输出、stop，服务0、质量驱动1。原72份
记录与新增96份记录完整后才分析；408份此前操作数/元数据相同。
MTP关闭，接受二进制2392f6b1不变，无新性能验收。

prefill raw写入源等于0..4095缓存；decode第4096项同新写入源，
前4096项不变。首decode位置4096未完成新的4-token组，所以1024
个compressed key应不变，实际确实位同；本轮不验证位置4099等
新组完成时的decode更新。decode评分核实际query同规范化核输出，
实际ck同压缩缓存；两阶段评分输出均逐字节同selector实际scores。

## 缩放与参考的区别

prefill实际点积先BF16物化，IndexerReduceKernel再按4个query头
顺序ReLU/FP32求和，乘rsqrtf(hd)。初始CPU参考用数学1/sqrt(128)
舍入FP32，有11447个可见score位差。保留该结果，再以独立设备
标量核传入运行时hd128，记录：

- rsqrtf(128)的FP32位模式1035273458。
- 1/sqrtf(128)的FP32位模式1035273459。

两者相差1 ULP。仅替换prefill缩放来源后，可见12288项及mask
12288项全部位同。不是改变参考容忍度，也未修改生产常数。

pooled decode直接按128维顺序FP32 fma计算每头点积，不物化BF16
点积；4头ReLU求和后使用1/sqrtf(hd)。独立CPU显式fma、关闭隐式
收缩、CPU平方根缩放，在本样本全部24576分数位同。高精度score
参考仍分别与prefill12288、decode6123个可见值FP32不同，完整留档；
两阶段本来有不同中间精度，不能据此直接认定任一路径错误。

prefill实际S矩阵同FP64(query×compressed key)舍入BF16有4项
差异。此处query/ck由前序核快照提供，尚未独立绑定实际cuBLAS
操作数和算法，因此只作为待核对参考，不能宣称GEMM已完整闭合。

## 压缩key参考与限制

实际key norm权重逐字节匹配self_attn.indexer.k_layernorm.weight
[128]；theta1e7、epsilon按位同FP32 1e-6。每4个raw key用FP64
平均、中心式RMSNorm乘(1+weight)，前64维在组首位置做旋转，
后64维保留规范化结果，最终FP32→BF16最近偶数舍入。

prefill共有1680项不同，保存均值、规范化值、最终FP64结果与索引。
当前未复现FP32归约、设备rsqrt/pow/sin/cos、旋转fma收缩顺序，
不将这些差异直接归因为舍入或判定通过。本请求三轴rope表相同，
不能据此验证不同轴位置下的多模态旋转语义。原始query/key投影
来源也未独立计算。

证据目录`.q4t-work/e2e/qsa-indexer-reference-20260923/`：
indexer-reference.json、decode-reference.json、reduce-device-scale.json、
reduce-audited.json、previous-binding.json、artifact-binding.json及
observed-captures/。工具零警告、运行与审计完成，非零差异继续保留。
下一步复核压缩key指定算术，再绑定prefill点积算法与原始投影来源；
完整prefill GDN、PLE和输出头仍待覆盖，整体正确性未验收。

## 后续：压缩均值、归一化与旋转指定算术

复用已绑定HTTP快照，不改运行时或观测。CPU按四token顺序FP32
求和除4，平方后执行warp XOR16/8/4/2/1归约，再按四warp顺序
相加，FP32均方+epsilon。设备仅重建rsqrtf、powf及sin/cos，CPU
完成规范化乘法与旋转fma及BF16舍入，不调用生产BuildCompressedK。

最初预声明两种整式fma顺序：第一项融合或第二项融合。CPU数学
函数参考分别有958/959项不同，设备数学函数分别剩6/2项。前一种
的6项都在旋转后半区，后一种2项都在前半区，原结果完整保留。

读取接受二进制的BuildCompressedKKernel SASS，前半区3c10先
FMUL配对值与sin，3c20以FFMA当前值*cos减该结果；后半区6040
先FMUL配对值与sin，6050以FFMA当前值*cos加该结果。两分支
始终融合当前值*cos，但源表达式中它的位置不同，所以统一按源
表达式“第一项/第二项”融合都不足以重建编译后行为。
初次用未修饰函数名查询未找到，随后按完整mangled符号提取指令，
保留查询输出与实际SASS；没有根据剩余差异直接挑选容忍规则。

新增SASS支持的分支顺序后，CPU数学函数仍有956项不同；设备
数学函数+CPU均值/归约/乘法/旋转则1572864个输出全部逐位一致。
六个变体分别958/959/6/2/956/0，前四个结果与原记录逐项相同；
旧FP64的1680项差异及全部变体输出/索引保留，不以较小值代替验收。
所有工具零警告，执行退出0，最终12份原始输出逐字节复核。

这是指定硬件数学近似与CPU算术下的局部重建，不是独立于CUDA
数学库或编译器精度选择的模型合同证明。本轮仅prefill1024组×12层，
三轴rope位置相同；不覆盖decode新组完成或多轴不同位置语义。
原始key/query投影与prefill评分GEMM来源仍待绑定，原错答未修复。

证据目录`.q4t-work/e2e/qsa-compression-reference-20260923/`，包含
initial-compression-reference.json、sass-order-reference.json、
sass-order-decision.json、compressed-k-exact.sass、input-binding.json、
artifact-binding.json及各变体输出。下一步补prefill评分GEMM实际
算法/操作数，再连接原始indexer及全注意力投影与位置变换来源。

## 后续：prefill评分GEMM实际操作数与算法重放

新观测以规范化query和压缩key核的实际输出指针识别评分调用，
逐层检查BF16列主序布局、FP32 compute/scale、默认epilogue、
A转置/B不转置、alpha1/beta0及32MiB workspace。记录每次实际
算法，捕获M16384/N2048/K128矩阵的最后4个query行、全部2048个
key行、对应4×2048输出，不只从前序函数推测GEMM读取对象。

零警告构建后首项三侧tools/evalscope原4K HTTP仍600440、4096
输入/7输出、stop，三服务0、质量驱动1。12份GEMM完整，780份
此前实际快照/元数据逐字节不变，才进行参考分析和独立重放。

实际最后4个query逐字节同IndexerQueryNormRope输出；前1024个
可见key逐字节同compressed缓存；GEMM输出逐字节同IndexerReduce
实际S输入。以这些直接捕获操作数重算49152个可见点积，FP64经
FP32→BF16仍有原4项不同，差异索引及参考值完整保留。

独立进程使用记录算法，AlgoCheck验证兼容性，cuBLAS130501，
保持矩阵形状、布局、全部2048 key和32MiB workspace。将捕获的
4个query行循环复制成16384行，只核对最后4行；不是原始全行
输入。12次、98304个BF16结果全部同HTTP，原始文件逐字节再核对。
因此原4项可在脱离模型调度的同算法GEMM中复现，未独立证明cuBLAS
内部累加算术。后1024列为因果mask不可见列，只为完整重放保留，
不对其赋予模型分数语义或建立高精度通过结论。

证据目录`.q4t-work/e2e/qsa-indexer-gemm-20260923/`，含三侧HTTP、
gemm-reference.json、replay-result.json、previous-binding.json、
artifact-binding.json及全部实际操作数。工具零警告、执行/审计退出0。
生产和接受二进制未改，原4K质量失败仍在。下一步连接原始indexer
query/key投影、query归一化/RoPE以及全注意力投影来源。


## 后续：query归一化与部分RoPE

新观测在IndexerQueryNormRope实际调用前捕获最后prefill/首decode
的4×128 query、128项归一化权重、theta/epsilon、实际位置及三轴
rope值、旋转维度和布局参数。零警告构建后第一项仍为三侧原4K
HTTP，均600440、4096输入/7输出、stop，服务退出0、质量驱动1；
24组新输入完整、此前840份快照/元数据逐字节一致后才数值分析。
这通过的是观测不改变该请求的检查，不是质量验收。

实际权重逐字节同checkpoint的indexer.q_layernorm.weight；4头、
每头128、旋转64维，theta=1e7、epsilon为FP32的1e-6。实际位置
4095/4096与先前捕获rope表一致，本轮三轴位置相同。

纯FP64归一化/旋转、仅最终舍入，在12288输出中有922项不同。
按生产合同在归一化后先舍入BF16，再用FP64旋转，剩7项不同。
两类参考及全部差异索引保留，不以FP64计算忽略中间舍入来判错。

指定算术参考使用CPU平方、warp XOR16/8/4/2/1和四warp顺序归约，
设备仅提供rsqrtf/powf/sin/cos；归一化乘法、BF16中间舍入、旋转
fma和最终舍入仍由CPU完成。预声明六种数学函数/乘加顺序变体，
差异数依次9/10/0/0/9/0。三个设备数学变体在本样本全部相同，
因此不能根据输出推断编译器到底融合哪项。

接受二进制SASS另存：07a0先将归一化值转BF16，07b0写入、07c0
同步；1be0将配对BF16移入FP32位表示。1bf0/1c00先算sin*b及
sin*a，1c10/1c20融合cos*a减前项、cos*b加前项。因此选择两半区
均融合当前值*cos的variant5有独立指令依据，12288输出逐字节同
HTTP。并不把其他变体同样零差异解释为其编译顺序也成立。

工具零警告、参考与审计退出0；生产代码和接受二进制未改，无性能
验收或整模型正确性结论。原4K错答仍在，原始query/key投影尚未
独立确认，且不覆盖多模态三轴不同位置或后续decode位置。
证据在`.q4t-work/e2e/qsa-query-transform-20260923/`，含三侧HTTP、
fp64-reference.json、query-reference.json、query-transform.sass、
sass-order-decision.json、previous-binding.json、artifact-binding.json。
下一步连接原始indexer query/key投影，再补全注意力投影与变换来源。


## 后续：原始query/key投影与HC输入绑定

在已识别WriteKV调用后开始捕获紧邻的indexer投影，检查query
512行、key128行、共同输入K2560和key权重地址等于query权重
地址加512×2560。随后在query归一化和raw-key写入的实际调用处
验证输出指针，不仅按相同形状猜测调用身份。prefill另记录实际
cuBLAS算法与描述符；decode捕获实际Bf16Gev输入/权重/输出。

观测零警告构建后首项三侧原4K tools/evalscope HTTP均600440、
4096输入/7输出、stop，三服务0、质量驱动1。48次投影观测完整，
此前960份快照/元数据逐字节不变，再开展离线参考。

每层实际两块权重均逐字节匹配checkpoint中
indexer.index_qk_proj.weight [640,2560]的前512/后128行。两阶段
24个query/key共同输入均同此前已检查HC attention混合输出。
这是跨观测运行的字节绑定；query原始输出同当前归一化前输入，
key最后行同当前raw写入输入，后两者同时具有本轮实际指针证据。

FP64点积再FP32→BF16，在prefill7680输出中4项不同：L23 key72，
L27 query493、L39 query277、L47 query441；decode7680项全同。
全部高精度值、舍入结果及差异索引保留。

Decode独立CPU按两个fma累加链、每lane八元素分组、warp
shuffle-down16/8/4/2/1顺序重建，7680项全同。Prefill独立进程
使用记录算法和原M4096/N512或128/K2560布局、32MiB workspace，
AlgoCheck通过，将实际最后一行输入重复4096次后检查最后一行，
24次7680项全同，cuBLAS130501。这复现了原4项FP64差异，但
不等于独立证明cuBLAS内部算术，重复行也不是原完整prefill输入。

首次分析在读取旧HC哈希清单时用错键层级，KeyError发生于数值
结果生成之前，脚本和错误日志保留。修正为先校验previous-binding
清单摘要，再按该清单逐项验证HC输出；没有重跑HTTP挑选结果。
最终参考与审计退出0，所有构建零警告，运行时和接受二进制未改。

本轮接通的是最后prefill/首decode样本的索引器链路，不覆盖完整
prefill所有行、后续decode新组完成或多轴不同位置。原4K错答仍在，
不宣称整模型正确或性能验收通过。证据目录
`.q4t-work/e2e/qsa-projection-reference-20260923/`含三侧HTTP、
projection-reference.json、decode/prefill-reference.json、
hc-input-binding.json、previous-binding.json及artifact-binding.json。
下一步转向完整注意力q/gate/k/v和输出投影、q/k归一化与RoPE来源；
全prefill GDN状态和PLE等未核对链路仍不能省略。
