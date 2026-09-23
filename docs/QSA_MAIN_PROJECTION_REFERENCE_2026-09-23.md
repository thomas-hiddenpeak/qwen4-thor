# 主注意力投影来源与算术核对（2026-09-23）

## 结论与范围

生产代码与接受二进制未改，原4K质量题仍600440而非360284，
不宣称整模型正确。12层最后prefill token4095和首decode token4096
的q/gate、k、v、out共96次投影实际权重匹配checkpoint；decode
独立CPU和prefill记录算法重放各190464输出逐字节同HTTP。

这些结果连同已核对的q/k变换、索引器、选块/KV交接及attention
计算，补齐这两个token的主注意力局部链路。它们仍不是完整prefill
各token、后续decode或整个模型的独立数值证明，不能覆盖原质量失败。

## E2E门禁与输入来源

新观测先零警告构建，第一项测试为tools/evalscope原4K无观测/
有观测/无观测三侧HTTP，均4096输入、7输出、stop、600440；三
服务退出0，质量驱动退出1。96组投影完整且检查通过，1632份此前
快照/元数据逐字节不变，再启动离线分析。诊断计时不参与性能比较。

qg投影N12288/K2560作为起点，紧随的k/v均N512/K2560且共同
输入；三组输出指针分别绑定QKDeinterleaveNorm和WriteKV实际
参数。out为N2560/K6144，以前序SparseAttention实际输出指针
识别，避免误捕获同形状linear输出投影。prefill M4096，decode M1。

所有实际权重逐字节匹配checkpoint中的self_attn.q_proj/k_proj/
v_proj/o_proj.weight。qg/k/v输入同本轮indexer投影输入，亦同
此前已核对HC attention混合输出；out输入同实际attention输出，
out结果同此前HC融合写入的block输入。跨运行的HC边界均校验
旧哈希清单摘要及逐项字节；不冒充本轮捕获了HC消费指针。

qg输出同本轮归一化前qg，k输出同本轮归一化前k，v输出同当前
WriteKV最后一行，之前的变换与缓存证据因此可串联。

## 数值结果与限制

FP64点积经FP32→BF16与实际输出差异如下，完整数值和索引保留：

| 阶段 | q/gate | k | v | out | 合计 |
|---|---:|---:|---:|---:|---:|
| prefill | 152 | 9 | 11 | 178 | 350 |
| decode | 3 | 0 | 0 | 14 | 17 |

Decode CPU复现两个fma累加链，每lane按八元素块顺序累加，再
shuffle-down16/8/4/2/1归约、乘alpha1并BF16舍入；48次190464
输出全部一致，解释上述17项与高精度点积不同的运行时结果。

Prefill捕获实际算法，核对BF16列主序、FP32 compute/scale、默认
epilogue、A转置/B不转置、alpha1/beta0及32MiB workspace。
独立进程cuBLAS130501通过AlgoCheck，以原M/N/K和布局重放；
将实际最后一行输入重复4096次，仅核对最后一行，48次190464
输出全部一致。原350项差异可脱离模型调度复现；这不是原始全行
输入，也不独立证明cuBLAS内部累加算术，不据此声称FP64相同。

## 证据与后续

证据目录`.q4t-work/e2e/qsa-main-projections-20260923/`，含三侧
HTTP、projection-reference.json、decode/prefill-reference.json、
hc-binding.json、previous-binding.json、artifact-binding.json及
实际操作数与所有参考输出。所有工具零警告，捕获控制、参考、
审计退出0。未改生产，无新增性能验收结论。

下一步检查PLE引入的L0输出到L1输入非恒等边界，包括hash/SSD
查表、FP8转换及卷积/加法；全prefill GDN状态独立生成、最终头部
等未确认链路仍保留，不能因主注意力局部对齐而省略。
