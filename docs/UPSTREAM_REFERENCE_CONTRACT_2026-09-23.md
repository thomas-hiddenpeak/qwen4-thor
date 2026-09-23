# MoE上游状态的独立参考合同

代码核对时间2026-09-23；本文件描述现有路径和验证缺口，不把注释
中的旧性能/误差声明当成当前验收证据。生产运行时未改。

## 实际依赖

```mermaid
flowchart LR
  T[四路BF16 trunk] --> P[仅第1层 PLE增强]
  P --> R[attention GRRead]
  R --> A[GDN或QSA attention]
  A --> W[残差写回及MLP GRRead]
  W --> M[已采样的MoE输入]
  M --> E[MoE]
  E --> O[MLP残差写回]
  O --> N[下一层trunk]
```

图中层号从0开始，非PLE层直接从trunk进入attention GRRead。
源码`decoder_layer.cu:345`的层类型为l%4==3全attention，其他36层
linear attention；PLE仅在l==1。核心调用见DecoderLayerForward：
先PLE、attention GRRead、attention、WriteAndRead、MoE、MLP Write。
因此MoE参考的实际输入不是层入口trunk，不能据MoE一致推断上游正确。

## HTTP服务的路径选择

`chat_server.cpp`单个prefill请求经ModelPrefill；普通decode无论B是否
为1，scheduler都调用ModelDecodeBatchMulti，传入真实seq_ids。
不能用独立ModelDecodeStep路径代替HTTP路径的证明。

`linear_attention.cu`默认单序列prefill走GdnRegPrepNormKernel及
GatedDeltaNetRegKernel<8>，归一化Q/K先存成BF16；pooled decode走
GatedDeltaNetDecodeKernel，Q/K归一化保存在FP32共享内存，递推累加
顺序亦不同。两者数学目标相同不等于逐位相同；旧路径统一实验
已拒绝的事实继续保留，不在本轮重新启用。

全attention存在不同indexer和稀疏attention分支：单序列prefill可用
indexer BF16 GEMM，pooled单行可能用IndexerDecodeScores；T=1的
QSA使用QsaDecodeSplit，其余使用SparseAttentionKernel。实际分支
还受max_blocks与长度影响，不能根据T=1推断所有上下文都走同一路。

## 当前证据与未覆盖范围（2026-09-24 更新）

| 边界 | 必须保存的输入/状态 | 独立核对对象 | 当前限制 |
|---|---|---|---|
| GDN状态交接 | prefill最终S，decode前S，seq_id | 原样交接、形状和层身份 | 本次原4K首decode已验证 |
| GDN单步 | 实际conv后QKV、a/b、dt_bias/A_log、S前后、y | 归一化、衰减、delta、秩1更新和读出 | 首decode指定算术全同；原4K prefill初态至末态亦已独立递推，完整卷积已接通，全行输入投影已绑定/重放，HC混合输入仍为实际数据 |
| linear输入投影 | 全行mixed输入、QKV/z/a/b权重/输出、算法 | 权重身份、矩阵布局/重放、FP64参考及下游全行绑定 | 36层4096行已捕获/重放，FP64差异保留；HC混合输入仍为实际数据，z下游全行已绑定NormGate |
| linear输出端 | 全行GDN输出、z、norm/out权重及输出、残差消费者 | NormGate指定算术、输出投影重放/FP64及实际消费 | 原4K36层全位置已核对；设备基础数学/cuBLAS不是完全独立的硬件精度证明 |
| linear短卷积 | 原始QKV、卷积权重、3项历史、输出及新历史 | 因果窗口、激活及状态顺序 | 原4K完整36层4096位置及首decode窗口已核对；原始QKV已绑定全行输入投影，其他请求未覆盖 |
| GDN prefill | 初始S、全序列QKV/a/b、norm前后和最终S | 全递推与BF16中间边界 | 36层4096位置的q/k归一化及全递推同指定参考；非线性复用设备数学函数，完整卷积与全行输入投影已接通，HC输入本身仍为实际数据 |
| MoE路由 | 完整HC MLP mixed、router权重/算法/logits、ID及路由权重 | 投影重放/FP64、Top10与softmax | 原4K48层全位置已核对；FP64舍入后103行集合不同，未建任务因果；全行mapping/gather、GU/down、shared及最终合并已核对；输入/中间量化确认E4M3尺度错误，未修正传播 |
| MoE mapping/gather | 完整ID/counts/offsets/lists/逆映射、实际x与input_scale、FP4及物理/逻辑SF | 列表覆盖、专家归属、逆映射、尺度身份与布局 | 原4K48层全位置及combine消费映射已核对；完整输入量化参考已完成：实际SF条件下FP4全同，但1208940项E4M3高区间编码错误确认；未修正传播 |
| QSA cache/indexer | 位置、RoPE、KV页表/新增槽、压缩索引、分数和top-k | 因果可见集、索引选择、softmax及attention | 12层全部4096 query的选择、KV及attention指定设备原语+CPU参考已核对；全query高精度及显式舍入差异已完整保留，完整评分/投影来源尚待补齐，压缩/indexer旧参考范围不能扩张 |
| PLE | token历史、hash参数、SSD行身份、反量化值、卷积历史 | 查表身份、门控投影、因果conv与trunk加法 | 全请求查表同SSD；完整prefill投影重放/FP64、norm/gate/广播及卷积指定算术已核对，完整交接已绑定；仍为逐算子实际输入条件参考，非独立组合前向 |
| GRRead/Write | 四路trunk、norm权重、投影/门控、写回BF16边界 | 混合、注入、残差与融合舍入顺序 | 48层末prefill/首decode权重、投影、混合已核对；完整4K残差、norm、mix、投影记录算法重放/FP64及门值已核对；FP64舍入差异保留，实际上游主干仍非独立全模型前向 |
| 最终head | 最终trunk、mix/norm/词表投影、logits及argmax | logit、排序与首处分歧 | 七次最终mixer/全词表/选择/下一步输入已核对；上游实际主干不等于从原始输入独立生成 |

参考分两层：独立高精度公式检查数学对象；按指定精度/顺序的参考
解释实际舍入。必须报告全部差异，不临时添加容忍条件；算术解释
不能覆盖任务质量退化。下一步补完整QSA评分/投影/变换来源与消费者，再核对组合前向，
同时保留原4K错误、已知E4M3尺度问题与其他请求尚未证明的限制。
不以局部算术一致或旧文档“已闭合”代替真实任务质量。

## 原4K状态交接结果

详见[GDN交接报告](GDN_STATE_HANDOFF_2026-09-23.md)。36层状态交接
逐位相同；单步FP64数学参考有20项BF16读出差异；后续按实际FP32
归约及运行时归一化重建后，状态及读出全部位同，数学参考差异保留。当前没有修复原错题，也没有接受任何运行时修改。

短卷积的新证据见[卷积交接报告](LINEAR_CONV_HANDOFF_2026-09-23.md)，
原始QKV投影仍为实际输入，不据此宣称整个linear层独立验证完成。

输入QKV/z/a/b投影的实际权重与末prefill/首decode参考见
[输入投影报告](LINEAR_PROJECTION_REFERENCE_2026-09-23.md)：decode指定
累加全同，prefill记录算法重放逐位相同，638项FP64差异保留；输入
已接通末行GRRead，但仍不代表全token投影或独立cuBLAS算术证明。

完整prefill归一化与递推见[GDN完整参考](GDN_PREFILL_REFERENCE_2026-09-23.md)，
最终七步见[最终mixer](FINAL_MIXER_REFERENCE_2026-09-23.md)和
[输出头](OUTPUT_HEAD_REFERENCE_2026-09-23.md)。QSA证据见
[选择/attention](QSA_SELECTION_REFERENCE_2026-09-23.md)、
[indexer](QSA_INDEXER_REFERENCE_2026-09-23.md)、
[主投影](QSA_MAIN_PROJECTION_REFERENCE_2026-09-23.md)。PLE见
[查表](PLE_LOOKUP_REFERENCE_2026-09-23.md)、
[投影](PLE_PROJECTION_REFERENCE_2026-09-23.md)、
[卷积及门控](PLE_CONV_REFERENCE_2026-09-23.md)。HC见
[交接/归一化/混合](HC_HANDOFF_REFERENCE_2026-09-23.md)和
[投影](HC_PROJECTION_REFERENCE_2026-09-23.md)。这些报告的样本范围
不能互相放大；原4K错误答案仍未修复，生产运行时未改。

完整4K短卷积的后续证据见[完整卷积报告](LINEAR_PREFILL_CONV_REFERENCE_2026-09-23.md)：
所有1509949440个输出同CPU累加+设备SiLU，接通完整GDN输入；
全token QKV/a/b投影的后续绑定与重放见[输入投影报告后续](LINEAR_PROJECTION_REFERENCE_2026-09-23.md)，HC输入来源仍待补齐，linear输出端全行见[输出报告后续](LINEAR_OUTPUT_REFERENCE_2026-09-23.md)。

完整MoE路由与FP64选择边界见[2026-09-24报告](MOE_ROUTER_REFERENCE_2026-09-24.md)。

完整mapping/gather边界与输入尺度见[2026-09-24报告](MOE_GATHER_REFERENCE_2026-09-24.md)。

完整输入量化的高区间错误及独立参考见[2026-09-24报告](MOE_INPUT_QUANT_REFERENCE_2026-09-24.md)。
