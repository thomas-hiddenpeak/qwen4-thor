# PLE实际token上下文与SSD行内容核对（2026-09-23）

## 结论与范围

本轮原4K请求的4096个prefill token和6步普通decode，合计4102个
实际输入token，对应65632次行读取、5560个不同SSD行。实际进入
FP8转换核的10501120个字节全部同独立计算预期行号后读取的磁盘
内容；同样数量的转换和缩放输出也同独立参考。

接受运行时未改，三侧HTTP仍600440而非360284，整模型正确性
尚未确认。没有直接捕获CPU内部行号，证据是“预期行的内容与
实际返回字节一致”，不能推导内部行号唯一或所有上下文均正确。
此前PLE后续投影/门控/卷积只验证末10个prefill/首decode样本，
本轮扩大的全请求覆盖范围仅限token→查表内容→转换→缩放。

## 捕获与HTTP门禁

最小观测以实际EmbedLookupKernel读取的token_ids记录输入，以
Fp8ToBf16Kernel输入/输出及ScaleBf16Kernel实际参数记录PLE值。
检查一次4096-token prefill后逐步单token，转换数量等于token数
乘2560，转换→缩放指针及调用次序一致；不从文本重新分词猜测
模型实际输入，也不从响应字符反推decode token。

必要观测构建零警告后第一项为tools/evalscope原4K三侧HTTP，
均4096输入、7输出、stop、600440，服务退出0、质量驱动1。7步
各tokens/convert/scale三组元数据共21组完整，全ok后才离线分析。
已验证的末10个prefill/首decode原始FP8、转换BF16、缩放BF16
各快照逐字节不变，并核验旧证据哈希。

## 独立上下文、参数与磁盘内容

读取checkpoint的layer_multipliers [3]、ngram_heads_vocab_sizes
[16]、ngram_heads_offsets [16]，以及BF16 weight_scale；保存原始
参数字节、形状和SHA。配置ngram_size3、每阶8头、总embedding
维度2560、每行160字节，EOS248044。

Python参考按已捕获的实际token顺序重建历史，序列开头用EOS
填充；历史位置早于最近一个既往EOS之后的片段时也用EOS。随后
对2/3-gram分别用整数乘法和XOR混合，再按各头词表取余加offset。
这是按片段边界构造上下文的实现，没有调用生产ComputeNgramRowIds。
本次没有实际EOS输入token，因此覆盖开头填充，但未覆盖序列内部
EOS重置。全部token×multiplier非负且小于2^63，最大
5880121689674598061，样本中不存在signed/unsigned溢出分歧。

每个预期行号严格限制在对应头的逻辑范围，按row_id×160只读
读取SSD文件，与转换核实际输入逐字节比较。重复行只读一次，
但全部65632次出现都参与比较；5560个不同磁盘行另存SHA，并在
最终审计中再次读取核对。数据文件前后device/inode/size/mtime
一致。磁盘路径来自接受代码的服务配置与保存的启动参数；尝试
读取观测服务FD时服务已退出，没有取得运行时FD记录，因此不
宣称有此项额外证据，也没有为此重复选择HTTP结果。

初始参考误要求文件长度等于逻辑词表总行数×160，断言在磁盘
行数值比较前失败。逻辑行数为320001446，而配置明确要求按128
行对齐；参考实现也按该公式构造embedding。物理行数320001536，
多90行、14400字节，与实际文件51200245760字节精确一致，并与
接受ModelConfig的ple_total_rows相同。按配置修正严格长度检查，
保留初始脚本、断言和参数目录；所有查表行号仍只能落在逻辑范围，
未扩大允许行号或引入误差容忍。

## 转换、缩放与证据

对全部实际FP8字节按E4M3FN符号/指数/尾数独立转换并BF16舍入，
再乘checkpoint weight_scale后RNE BF16，均同实际输出。没有NaN
或指数15输入编码，所以不外推为全部编码测试，也不意味着此前
其他量化器的E4M3高范围错误已修复。

证据目录`.q4t-work/e2e/ple-lookup-reference-20260923/`含原始
HTTP/token/FP8/BF16、checkpoint-parameters.json、sidecar-identity.json、
lookup-reference.json、selected-row-hashes.json、所有预期行号/内容、
effective-contexts.i64、previous-binding.json、source-binding.json和
artifact-binding.json。观测零警告、参考与审计退出0；生产未改，
没有新的性能验收结论。

本请求未发现PLE查表内容或历史拼接不一致。下一步核对模型输入
embedding及最终输出头；全prefill GDN状态独立生成、长上下文、
分块prefill、多流/MTP等未验证范围仍不能用本结果覆盖。
