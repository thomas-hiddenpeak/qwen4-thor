# 模型embedding与四分支入口核对（2026-09-23）

## 结论与边界

原4K请求的4096个prefill token和6步普通decode，合计4102个
实际输入token，使用96个不同embedding权重行。所有实际权重行
逐字节同checkpoint；10501120个embedding BF16和42004480个
四分支展开值全部同独立索引/复制参考。第一层HC实际输入指针及
各次末行内容与展开结果接通。

接受运行时未改，原质量题仍600440而非360284，整模型正确性
尚未确认。仅本请求、单流、MTP关闭、纯文本入口，不外推到视觉
embedding注入或其他请求。第一层HC本轮核对的是输入指针/形状
和末行，未重新验证全部token的HC归一化算术。

## HTTP观测与实际权重

在上一轮token/PLE转换观测上增加实际embedding权重行、embedding
输出、ExpandTrunk输出，以及其后首个HC GroupedRmsNorm的输入
检查。按model_head/hyperconnection源文件标识匹配kernel，避免
PLE/MTP同名核。每次embedding后只等待一次首层HC输入，不将
prefill后的最终T1 mixer误当首decode。

必要构建零警告后首项三侧tools/evalscope原4K HTTP均600440、
4096输入/7输出、stop；服务退出0、质量驱动1。7次调用的新增
embedding/expand/entry与此前tokens/convert/scale共42组元数据
完整且全部ok后，才开展数值分析。此前56份token和PLE原始记录
逐字节不变，不把输出不变称作质量验收通过。

从EmbedLookup实际ids参数读取token，去重后按其实际权重基址
捕获这些行；要求所有ids在[0,248320)。捕获实际行号表与BF16
权重行，离线逐行核对model.language_model.embed_tokens.weight
BF16[248320,2560]的对应磁盘区间。保存checkpoint路径、tensor
偏移、文件身份及96行SHA，最终审计再次读取磁盘行核验。
不是单凭kernel输出像embedding就认定权重来源正确。

## 索引与分支排列

参考根据实际ids，从已核对的实际权重行表按token顺序索引，
结果与完整embedding输出逐字节相同。ExpandTrunk实际输入指针
同embedding输出，T与当前token数相同，hc4、hs2560。其输出
按[T,4,2560]解释，分别核对四个分支，每个分支均逐字节等于
embedding[T,2560]，共42004480项，无算术容忍阈值。

ExpandTrunk后第一次HC norm的输入基址、T/hc/hs匹配展开结果，
并保存其末行；每次末行均同实际展开结果末行。prefill末行与首
decode还匹配此前HC观测的L0 read.trunk，旧清单摘要和条目SHA
均检查。后五个decode入口的字节/指针证据来自本轮捕获，不借用
只覆盖首decode的历史HC结果。

## 证据与后续

证据目录`.q4t-work/e2e/model-input-reference-20260923/`包含
三侧HTTP、全部实际输入和权重行、input-reference.json、
checkpoint-row-binding.json、hc-binding.json、previous-binding.json、
artifact-binding.json和按权重行独立索引的embedding结果。
构建零警告，捕获控制、分析及审计退出0；生产未改，无新的性能
验收结论。

本请求未发现输入embedding索引或四分支排列错误。下一步核对
最终mixer、lm_head和greedy token选择；全prefill GDN状态的
独立生成仍是未确认项，不能由入口和PLE正确覆盖。
