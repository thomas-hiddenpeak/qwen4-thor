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

## 待建立的合同

| 边界 | 必须保存的输入/状态 | 独立核对对象 | 当前限制 |
|---|---|---|---|
| GDN状态交接 | prefill最终S，decode前S，seq_id | 原样交接、形状和层身份 | 本次原4K首decode已验证 |
| GDN单步 | 实际conv后QKV、a/b、dt_bias/A_log、S前后、y | 归一化、衰减、delta、秩1更新和读出 | FP64公式及指定FP32单步已建立，实际初态仍未独立生成 |
| linear短卷积 | 原始QKV、卷积权重、3项历史、输出及新历史 | 因果窗口、激活及状态顺序 | 未采集 |
| GDN prefill | 初始S、全序列QKV/a/b、norm前后和最终S | 全递推与BF16中间边界 | 未独立重建；状态交接不证明其值正确 |
| QSA cache/indexer | 位置、RoPE、KV页表/新增槽、压缩索引、分数和top-k | 因果可见集、索引选择、softmax及attention | 未建立本请求对应参考 |
| PLE | token历史、hash参数、SSD行身份、反量化值、卷积历史 | 查表身份、门控投影、因果conv与trunk加法 | 未建立本请求对应参考 |
| GRRead/Write | 四路trunk、norm权重、投影/门控、写回BF16边界 | 混合、注入、残差与融合舍入顺序 | 当前MoE输入被当成实际边界，尚未独立生成 |
| 最终head | 最终trunk、mix/norm/词表投影、logits及argmax | logit、排序与首处分歧 | 本轮未覆盖 |

参考分两层：独立高精度公式检查数学对象；按指定精度/顺序的参考
解释实际舍入。必须报告全部差异，不临时添加容忍条件；算术解释
不能覆盖任务质量退化。优先补GDN单步，再向conv和prefill初态追溯。
其余项保持未验证状态，不以旧文档“已闭合”代替真实HTTP证据。

## 原4K状态交接结果

详见[GDN交接报告](GDN_STATE_HANDOFF_2026-09-23.md)。36层状态交接
逐位相同；单步FP64数学参考有20项BF16读出差异；后续按实际FP32
归约及运行时归一化重建后，状态及读出全部位同，数学参考差异保留。当前没有修复原错题，也没有接受任何运行时修改。
