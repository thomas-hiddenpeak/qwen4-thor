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
