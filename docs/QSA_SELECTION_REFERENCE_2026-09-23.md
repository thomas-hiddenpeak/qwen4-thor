# 全注意力选块、KV交接与FP64参考（2026-09-23）

## 当前结论

原4K请求12个全注意力层，prefill末token/首pooled decode共24次。
49164个有效选择索引同独立CPU比较网络的完整顺序，且所选分数
不低于未选分数；因果范围、唯一性、尾部填充与attention实读列表
均一致。50343936个写入K/V BF16与注意力读取前缓存逐位一致，
50331648个prefill历史缓存值在首decode保持不变。

这只证明给定实际scores及实际变换后K/V时的选块和缓存交接。
索引分数与Q/K/V投影、归一化/RoPE来源均未独立证明。

独立FP64注意力参考共147456个BF16输出，有43122项不同：
prefill21904，decode21218。最大绝对差0.006585754352091522，
最大层/阶段相对L2差0.002430834388758418。全部差异索引与数值
保留，没有设容忍阈值或认定其合理。实现的分块在线softmax、BF16
概率舍入、Tensor Core累加及设备exp尚未逐项解释。
生产未改，原4K答案仍600440（应360284），整体正确性未通过。

## 实际路径与观测

代码full_attention.cu在此长度走short top-k：prefill无seq_id，
索引分数由BF16 GEMM后reduce；pooled单流decode有seq_id，使用
IndexerDecodeScores。两者调用ShortTopkSelectKernel。
prefill调用SparseAttentionKernel，首decode调用
SparseAttentionDecodeSplitKernel，形状nq24/nkv2/hd256。
观测通过实际核句柄及调用次数分别绑定12层，层号3/7/.../47。

新只读观测零警告构建后第一项执行tools/evalscope原4K无/有/无
HTTP。三次全文600440、4096输入/7输出、stop，服务0、质量驱动1。
72份write/select/attention元数据完整后才运行低层分析。
MTP关闭，接受二进制2392f6b1不变。只通过本请求观测不变性，
不是质量验收或性能测试。

捕获prefill全部4096条实际WriteKV输入K/V与位置；首decode一条。
attention执行前捕获对应末行Q/gate、选择列表、长度、页表及实际
物理缓存，执行后捕获输出。decode要求实际seq_id为0；prefill无
seq_id。页大小16，物理slot=page_table[pos]*16+pos%16。
本样本所有页表为恒等映射，但分析按捕获页表寻址，不仅比较连续地址。

## 选择与缓存参考

位置4095/4096均有1024个完整4-token块，选512块。prefill选择
2048 token，decode再追加未完整组的当前token4096，长度2049。
剩余2052容量中的槽必须为-1。列表不重复且全部在0..当前position。

CPU独立实现2048宽严格比较网络，分数相等不交换，padding为-1e30。
它验证完整选择顺序；另检查所选最低分>=未选最高分，不把网络复制
本身当作选块数学性质的唯一证明。8个prefill层在选择最低分处存在
相同分数，不能随意换成另一种排序的ID顺序。实际传入attention的
列表与长度逐字节同selector输出。

所有WriteKV位置匹配prefill0..4095/首decode4096，实际输入K/V
按页表映射后逐位同缓存。首decode逻辑0..4095全部同prefill缓存，
新增位置逐位同本次写入源。该核对没有独立生成K/V或证明其数学来源。

## FP64注意力参考与后续

从已验证列表和页表取实际选中K/V，每12个Q头共享一个KV头。
FP64 QK点积乘1/16，稳定softmax、加权V，再逐元素乘sigmoid(gate)，
最后FP32→BF16最近偶数舍入。保存每头score、概率、门前输出、
最终参考及全部差异索引，不通过重采样寻找相等输出。

下一步复现16-token在线softmax的更新和BF16概率舍入，进一步
定位43122项差异；并继续独立核对索引分数及其压缩key来源。
完整prefill GDN、PLE与输出头仍未覆盖，不把本轮局部检查扩大为
整模型正确性结论，也不据此排除实现对原错答的影响。

证据目录`.q4t-work/e2e/qsa-selection-reference-20260923/`：
raw-http-audit.json、selection-reference.json、attention-reference.json、
capture-binding.json、artifact-binding.json及observed-captures/。
两份分析均退出0表示计算和数据检查完成；attention差异明确非零，
不以进程退出码代替数值验收。仓库保存观测及两份独立分析模板。

## 后续：中间BF16舍入与在线softmax分解

复用已通过HTTP观测和选块/KV身份检查的冻结快照，不改生产或观测，
不重新采样请求。校验旧artifact-binding与全部原始capture-binding，
本轮另外绑定并复核所用参考数组；147456输出范围保持不变。

代码在概率进入PV之前舍入BF16，在线分母仍累加未舍入概率；还在
最终除以分母之后、乘sigmoid门之前，将注意力结果物化为BF16。
上轮纯FP64数学参考未模拟这两处中间舍入。因此43122项不同不能
直接视为实现错误数量，但也不因补舍入后更接近就验收。

| 参考变体 | prefill不同项 | decode不同项 | 总计 |
|---|---:|---:|---:|
| 原始全FP64参考，只有末端BF16 | 21904 | 21218 | 43122 |
| 全FP64，加门前BF16 | 6835 | 6088 | 12923 |
| 16-token在线FP64，不加中间舍入 | 21904 | 21218 | 43122 |
| 在线FP64，加概率BF16 | 19373 | 19035 | 38408 |
| 在线FP64，加概率与门前BF16 | 63 | 30 | 93 |
| 指定FP32在线近似，加两处BF16 | 67 | 35 | 102 |

所有变体均预先写入plan.json，没有改阈值或挑选93项作为通过结果。
纯FP64在线/整体softmax门前结果最大绝对差约1.07e-14，最终BF16
不同位置集合与原始参考相同。两处BF16共同加入时，原差异43052项
转为一致，仍有原差异70项，并新出现23项，合计93；不能说原43122
项中的43029项已逐项解释。FP32近似则保留原差异74项、新28项，
合计102，所有新旧索引及输出文件保留并独立逐文件核对。

FP32在线近似仍不是硬件逐位参考：QK用FP64点积舍入FP32；exp为
CPU FP64结果舍入FP32；分母按实际16-token顺序逐项FP32累加；
BF16概率与BF16 V的PV用FP64计算后舍入FP32；在线acc缩放相加
使用FP64乘加再舍入FP32，模拟融合操作但不主张替代所有极端值的
精确FP32 fma；门函数和最后乘法使用FP32，并保留门前BF16。
这些区别均明确记录，不能用FP64变体93项比FP32变体102项更少来
推断哪种实现更正确，更不能据此改变生产精度。

证据目录`.q4t-work/e2e/qsa-rounding-reference-20260923/`：
plan.json、rounding-reference.json、rounding-audited.json、
reference-binding.json、artifact-binding.json及各变体BF16文件。
两份CPU分析退出0仅表示执行/审计完成，93/102项仍未闭合。
下一步隔离Tensor Core的QK/PV点积与设备exp及在线累加顺序。
实际索引分数来源仍待独立验证；原4K质量失败及全模型未验收状态不变。
