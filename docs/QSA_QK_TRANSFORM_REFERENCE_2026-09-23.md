# 主注意力q/k变换局部核对（2026-09-23）

## 结论与边界

接受运行时未改。原4K HTTP质量题仍600440而非360284，整模型
正确性尚未确认。本轮仅12个全注意力层最后prefill token4095和
首decode token4096：实际gate拆分、q/k归一化、部分旋转及下游
交接已连接参考。不覆盖完整prefill、后续decode、多模态不同轴
位置，也未独立确认q/gate/k/v/out原始投影。

159744个归一化输出在CPU指定归约顺序下全部位同；159744个旋转
输出使用设备数学函数和CPU指定fma/BF16舍入后全部位同。147456
个gate值逐字节同qg每head后半区及attention实际gate输入。

## 观测门禁与绑定

新观测零警告构建后首项为tools/evalscope原4K无观测/有观测/
无观测三侧HTTP，均4096输入、7输出、600440、stop；三服务退出0，
质量驱动退出1，保留质量失败。观测72组完整且全部ok，1176份
此前快照/元数据逐字节一致后才开展离线数值分析。

捕获实际QKDeinterleaveNorm的qg、变换前k、两组norm权重、epsilon，
以及输出q/k/gate；q24头、k2头，每头256维。qg按[24,2,256]拆分，
前半区q、后半区gate。两组实际权重各256项逐字节匹配checkpoint
self_attn.q_norm.weight和k_norm.weight，epsilon为FP32的1e-6。

PartialRope的N24/N2实例分别识别；核对其输入指针直接来自前序
norm输出，旋转64维、theta1e7，实际位置4095/4096和三轴坐标与
先前rope表一致。本轮三轴相同。norm输出同旋转前快照；旋转后q
同SparseAttention实际输入；旋转后k同WriteKV最后一行输入。
这些连接不能替代投影输入来源或完整KV历史的验证。

## 数值参考

归一化：CPU按FP32平方、每warp XOR16/8/4/2/1、八warp顺序相加，
均方+epsilon；归一化使用(1+weight)，最后BF16舍入。CPU数学
rsqrt及独立设备rsqrt两个预声明变体均159744项全同。纯FP64
均方/归一化再FP32→BF16则有1项不同，值和索引保留。

旋转：使用实际norm输出作为输入，独立检查变换本身；CPU承担
fma和BF16舍入，数学函数有CPU和设备两组。预声明六个变体
分别融合源第一项、第二项和当前值*cos，差异数66/66/0/0/66/0。
FP64旋转再FP32→BF16有39项不同，全部记录。多个设备变体同样
零差异，不足以从样本反推编译器融合顺序。

接受二进制SASS另提供顺序依据：q的18b0/18c0先FMUL sin*b与
sin*a，18d0/18e0再FFMA cos*a减前项、cos*b加前项；k对应
18a0/18b0与18c0/18d0，同一顺序。BF16操作数从R9/R10读入并
扩展到R9/R7。选择每半区当前值*cos融合的设备数学变体有独立
指令依据，其159744输出逐字节一致。norm参考也已全同，因此
两段边界可连接；这不是独立于CUDA数学近似的精度证明。

## 证据与下一步

证据目录`.q4t-work/e2e/qsa-qk-transform-20260923/`包含三侧HTTP、
fp64-reference.json、transform-reference.json、q-rope.sass、
k-rope.sass、sass-order-decision.json、previous-binding.json、
artifact-binding.json及每层每阶段所有变体输出。工具构建零警告，
捕获控制、参考和审计退出0；生产未改，没有新的性能验收结论。

下一步核对主注意力q/gate/k/v/out实际投影权重、输入和累加结果；
之后仍须补全prefill GDN状态、PLE及最终输出等未确认链路。
