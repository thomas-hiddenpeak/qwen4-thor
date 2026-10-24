# 旧 trace memory 缺口的源码复核

2026-10-24。仅静态源码复核；没有新增 GPU、HTTP、profile、采样、测试或分析器
执行。读入已授权的一次旧 SQLite 查询输出 `memory-coverage-old.json`，其 SHA256
为 `597287851d26f2abed9fa776359d6837c320abb14b943086d301d5d2ef90f4a3`。
本文件不改变旧 trace 的 warning 失败身份，也不为新候选给出 PASS。

查询在目标进程发现 14283 次成功 `cudaMemsetAsync_v3020`，其中 7902 次关联
MEMSET activity，6381 次没有任何 kernel/copy/memset 的同进程 correlation。
缺口按最深已记录 NVTX owner 为 prefill 96、`mtp.forward` 2、verify 6216、
`mtp.attention` 67。6216 = 74 × 84。4177 次 async copy 和374次同步 copy 均
有同进程 MEMCPY activity。API表没有指针、字节数或调用栈参数，不能补造。

## 84 次/verify 的可检验候选

源码有一组恰好84次的小 N cuBLASLt投影：

- 模型默认48层（`include/q4t/model/model.h:52`），每第四层是full attention
  （`src/model/decoder_layer.cu:371`），即36个linear、12个full。
- linear的 `in_proj_a`、`in_proj_b` 各一次，N=nv=48、K=hs=2560
  （`src/model/linear_attention.cu:1502` 和1507；维度加载见
  `src/model/decoder_layer.cu:429`）。36 × 2 = 72。
- full的index-key投影N=128，query投影N=512
  （`src/model/full_attention.cu:1578` 至1586）；小N的key投影共12次。
- `Bf16SmallMShapeRouted()` 明确拒绝N<320
  （`src/model/small_m_gemm.cu:232` 至251）；T=4时上述72+12次会落到
  `Bf16Gemm` 的 `cublasLtMatmul`
  （`include/q4t/model/linear.h:158` 至179）。

这是源码次数与trace缺口总量相符的候选解释，**不是逐调用证明**。这些投影调用点
没有显式 `cudaMemsetAsync`；cuBLASLt内部实现及参数不在本仓库源码中。其他小N
路径也存在，因此不能仅凭总数认定每条缺口属于上述投影、推断bytes=0、判定合法
no-op或判定GPU activity丢失。未执行进一步邻接API/kernel查询。

直接应用memset另有非零操作：常规MoE先清routed输出
（`src/model/moe.cu:946`，T×2560×4 bytes），verify的M=4路由到
`MoEDeviceDecodeMulti`（`src/quant/moe_gemm.cu:353`），其routing清零为
512×4+4=2052 bytes（`src/quant/moe_decode.cu:644`）。不能把这些调用未经关联
就归入6381个缺口。decoder/mtp reset只应跟请求状态重置关联，也不以84计数猜测。

## 有界后续路线与当前边界

root已决定在原4capture、6HTTP预算内让首次fixture包含固定512/16384bytes
memset（32step偶/奇各一半），并独立核对CPU ledger与返回数据；本agent不执行。
该fixture只能检验已冻结的具体非零形状与采集能力，不证明所有cuBLASLt内部API
的活动语义。模型中仍有未解释memory缺口时标 `UNKNOWN_CAPABILITY` 并停止GPU
比较，不加大小/耗时阈值或总数白名单。结构错误（歧义correlation、应用kernel
缺失等）另列FAIL，不能把未知能力缺口与已证明结构失败合并。

若需后续针对性研究，先绑定cuBLASLt实际调用点/形状以及API邻接序列，再决定
是否值得独立的固定形状最小复现；这不在当前新增测量授权中。CPU wall、thread
CPU、API wall、GPU activity union继续独立报告，wall−threadCPU不是GPU时间。
