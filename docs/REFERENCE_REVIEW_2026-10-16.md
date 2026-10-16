# KVMem、NInfer、ActKV、Strata 源码评估

日期：2026-10-16。用户确认“Strada”指 `Niko1221/Strata`。
本项目审阅起点：`9f288233ee78567f6dc402fbc2e1f14a6825acc7`，
生产源码已恢复 main `28a3783`；本轮仅研究、更新文档。

## 结论

**当前 decode 优化优先参考 NInfer 的执行组织、Strata 的同模型实现；
未来多会话/冷存储优先参考 KVMem；ActKV 保留为近似算法研究。**
四者没有一个适合整体替换本项目。它们也不是四套完全独立的技术：
KVMem 当前接入经过 patch 的 NInfer，并吸收 ActKV 社区分支的部分机制。

| 项目 | 真正值得参考 | 当前直接接入价值 | 主要限制 |
|---|---|---|---|
| NInfer | GPU 接受/元数据、Graph 分桶、GDN Record/Fold、独立草稿 head | 执行机制高，现成 kernel 低 | 模型族、量化格式、固定 shape、SM120a 均不同 |
| Strata | 同模型 QSA 页驻留、MTP/lookup 草稿、专家缓冲所有权、会话恢复 | 同模型对照高，容量方案有条件 | 面向独显/CPU 协同，数值格式与 Thor 不同 |
| KVMem | native KV payload、spill/restore/publish 事务、会话版本与失败测试 | 未来会话管理高，当前 decode 低 | host 分层不增加 Thor 总 DRAM；检索不是原生 QSA |
| ActKV | 策略历史提交/回滚、页搬移计划、agent 近似研究 | 当前低 | 社区实现不等于论文；额外删 KV 改变计算合同 |

所有“高/低”是针对当前工程的判断，不是外部项目综合评分。
**本轮没有构建或执行第三方测试，没有运行 HTTP、GPU 测试或 bench；
没有新的速度、数值或任务质量通过结论。** 下文“测试覆盖”指读到的测试源码。

## 来源与审阅方法

先读本项目 AGENTS、STATUS、EVALUATION，再逐项核对外部实现、生产调用点、
测试与本地对应路径。检出只放在 `.q4t-work/reference-review-20261016/`，
未改 `reference/`、模型目录、生产代码或二进制。

| 身份 | 固定提交 |
|---|---|
| [KVMem 主仓](https://github.com/kvmem/kvmem) | `0af10ece3d78edb905c62357e52a62147df69d58` |
| [NInfer 上游](https://github.com/Neroued/ninfer) | `81c8ce093b2c1646a87566a8e59d807fcf0ec95c` |
| [Strata](https://github.com/Niko1221/Strata) | `296b59d2f73c8f04a805df29c2fa97655adace78` |
| [ActKV 社区 KVMem 分支](https://github.com/melis2023/kvmem-actkv) | `fe4c5d9d57f6714f2ec1d6657d237813d7ec1a89` |
| [ActKV 非官方 Python/Triton 复刻](https://github.com/Maurya30/actkv-open) | `0345bd76123f78b0c6ce525a28db8cb7d4e271dd` |

KVMem 的 [backend manifest](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/backends/versions.json)
固定 `iamwavecut/ninfer-all@8319e8f512477d0cfdef89e16c17c1ba9f9bc7b3`
并另加本仓 patch。不能把这个组合与上表 NInfer 上游混成同一个版本。

## 本项目决定了哪些收益不能照搬

1. **已经原生稀疏。** `src/model/decoder_layer.cu:389-425`、
   `include/q4t/model/full_attention.h:4-36`：48 层中 12 层 QSA，
   每层每 query 用学习型 indexer 选择最多 512 个四 token 组，展开最多
   2048 token，加未完成组尾部；短上下文保留全部可见位置。
   其余 36 层是固定大小的 GDN 状态。
   原始历史 KV 保留，是为了未来 query 还能选到它；不是每步 dense attention。
2. **Paged 不等于已经按需驻留。** 当前 16-token page table 为 identity，
   KV、idx_raw、idx_comp 按 `max_len × max_seq` 全额分配。
   `src/runtime/memory_budget.cpp:61-80` 明确主模型和独立 MTP 状态成本。
   当容量取 204800、单序列时，主模型 BF16 KV 为 4.6875 GiB；
   加 MTP KV 为 5.0781 GiB；再加当前 indexer/page-table/rope 数组约
   6.3622 GiB。这是源码尺寸计算，非实测峰值，不含固定状态与 workspace。
3. **统一物理 DRAM。** 完整 host KV 副本加 device cache 不能像独显那样
   增加一套独立 RAM 容量，且会增加副本和流量。真正节省容量需要消除副本、
   按需物理分配或 SSD 冷层；SSD KV 还会与既有 PLE/专家 I/O 竞争。
4. **已有高收益 MTP。** 当前默认 k3 已通过五档；GPU draft loop、
   batch verifier、SSM/conv/PLE checkpoint 都已实现。
   `src/mtp/mtp.cu:1004-1061,1125-1177` 仍有两次结果 D2H 和 host 接受决策，
   因而存在执行组织上的增量，但“也支持 MTP”本身不构成价值。
5. **本轮不重启已结束的优化。** 10 月 16 日两候选 NO_GO 保持不变。
   下文候选不算第三个自动试验，外部吞吐与召回数字不充当本机验收。

## NInfer：最有价值的是 MTP 状态与执行组织

### 1. 接受决策、下一轮元数据和有界 Graph

实际调用链在
[speculative/mtp.cpp:70-201](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/qwen3_5/program/speculative/mtp.cpp#L70-L201)：
device 保存 anchor/frontier/budget/extent，Forward 做 target verify；
Finish 用 GPU accepted_count 生成 alignment、位置、有效列及下一轮 draft。
[target_verification.cpp:27-47](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/qwen3_5/program/speculative/target_verification.cpp#L27-L47)
确实调用 GPU 接受与 hidden 选择，不是 host 算完后只上传结果。
[graph_profiles.cpp:9-49](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/qwen3_5/program/planning/graph_profiles.cpp#L9-L49)
按 context 资源包络分桶，同精确 batch/topology 复用图。

**增量：** 本地已消除逐 draft 回读，但还没把 verify/accept/extend 元数据
连成固定 device 接口；当前 `src/`、`include/` 没有 CUDA Graph capture。
可参考这种分阶段接口，减少提交与 host 等待。

**边界：** NInfer 并非整轮零 host。
[decode.cpp:461-475](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/qwen3_5/program/decode.cpp#L461-L475)
在两阶段之间处理 grammar，之后 synchronize；最终状态提交也在图外。
本项目 PLE 由 CPU hash/io_uring 驱动，不能把它直接塞进 capture，或删除刚修好的
pinned staging event 所有权保护。host gap 占当前 E2E 多大比例尚未测量。

### 2. Record/Fold：少存完整 SSM，接受后重放状态转移

[recurrent.cuh:675-697](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/ops/linear_attention/gated_delta_net/recurrent.cuh#L675-L697)
的 record kernel 在寄存器中计算 verify 输出，却不发布持久 SSM；
[gdn_replay_records.cpp:96-111](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/core/gdn_replay_records.cpp#L96-L111)
保存 BF16 conv/K/V 与 FP32 gates。
前端确定实际发布前缀后，
[prefill.cpp:299-343](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/qwen3_5/program/prefill.cpp#L299-L343)
提交 fold；[一次 launch 覆盖全部层](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/ops/linear_attention/gated_delta_net/recurrent.cu#L104-L126)。
record/fold 共用 recurrence 算术 helper。

本项目 `model.cu:1671-1686` 和 `linear_attention.cu:593-611` 为每个前缀
保存完整 FP32 SSM。按当前单序列 36×48×128×128、k3，额外 SSM checkpoint
为 **324 MiB**；按同维度 T4 的紧凑 conv/K/V/gates 设计约 **5.115 MiB**。
两者都是 payload 尺寸推算，不含持久 state、PLE、对齐与 workspace，
更不是 63 倍加速预测。
紧凑记录可复算为
`36×4×[2×(10240+2048+6144)+4×96]=5,363,712 B`；
这是将该设计映射到本模型的估算，不是NInfer现成36层实例的实测分配。

这是明确的内存写入优化机会，但 full accept 也需要重做 recurrence；
当前 full accept 不用 rollback。应比较节省的 checkpoint 写入与新增 fold
计算，保留独立 accepted-prefix 逐位状态/后继输出合同。
外部 fold geometry 只实例化 48层/48头、30层/32头，本项目 36层要适配，
PLE conv 必须另行正确处理。
[测试源码](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/tests/ops/test_gdn_replay_fold.cpp#L724-L745)
有独立逐 token snapshot 与连续轮次对照，值得参考但本轮未执行。

### 3. 只优化草稿 head

[proposal.py:45-95](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/tools/convert/proposal.py#L45-L95)
选频率排行 131072 行并保留 special tokens，单独 Q4 编码、保存 token 映射；
[运行时](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/qwen3_5/execution/text.cpp#L564-L590)
在草稿路径投影和映回全词表，target 仍用完整 head。
本地 `mtp.cu:1856-1867` 草稿仍读完整 BF16 head；本模型 head
248320×2560×2 约 1.184 GiB，存在带宽候选。
但短词表覆盖不足会降低接受率，收益需按每轮成本/接受 token 数衡量。
外部词频和 131072 阈值不能直接套用，衍生产物也不得写回只读模型目录。

### 不宜直接移植的部分

- [registry.cpp:8-16](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/registry.cpp#L8-L16)
  只注册 Qwen3.5 Dense/MoE 架构，没有 qwen4_exp 的 PLE/HC/QSA 路径。
- [MoE codec 白名单](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/ops/sparse_moe/decode/sparse_moe_decode_plan.cpp#L21-L29)
  是 Q4 gate/up＋Q5/Q6 down 或 MTP Q8/Q8；固定 2048/256专家/top8/512
  shape，与本项目 2560/512/top10/640 NVFP4 不同。它的 NVFP4 主要服务
  dense 固定形状，不能误称现成 NVFP4 MoE 优化库。
- [构建](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/CMakeLists.txt#L3-L12)
  与 [运行时](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/models/qwen3_5/program/planning/startup.cpp#L812-L813)
  都限定 SM120a/CC120。删 guard 不等于支持 Thor SM110a。
- [K8V4 append](https://github.com/Neroued/ninfer/blob/81c8ce093b2c1646a87566a8e59d807fcf0ec95c/src/ops/kv_cache/append/k8v4_kernel.cuh#L22-L67)
  确有 Hadamard、FP8 K、FP4 V；codec 使用可表示 scale 后再量化。
  这是主模型数值格式变更，应与保持现有 target 精度的组织优化分开。

## Strata：同模型参考价值最高，重点看三个具体机制

### 1. 原生 QSA 选块与物理驻留分离

[verify.cpp:1416-1427](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/core/verify.cpp#L1416-L1427)
先 QSA score/top-k，再用同一 `sel_` 做 resolve 和 attention。
[kv_stream.cu:89-175](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/kernels/cuda/kv_stream.cu#L89-L175)
在 GPU 上给当前访问页打 epoch，atomicCAS 去重 miss，CLOCK 选择本轮未使用
的 victim，然后从 mapped host 读完整页到 device slots；16字节矢量复制。
不同 verify query 所需页一起保护，不因 cache miss 改掉 QSA 选择。

[layer.cpp:798-830](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/core/layer.cpp#L798-L830)
为全部逻辑 K/V 分配 host 权威副本；append 始终写 host，命中时同步写 slot。
合法容量配置下这是存储变换；容量不足仍有 overflow 标志，不能省略容量证明。
[kv_stream_parity.cpp:184-212](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/kernels/kv_stream_parity.cpp#L184-L212)
比较 resident/streamed attention 的字节，并验证页表双向映射、命中与 miss。
覆盖其内部相同 codec，不证明与本项目 BF16 逐位相同。

**建议：** 将来真做 KV residency，优先借这个“QSA 决策不变，只搬页”的边界，
而不是先用启发式永久删块。本项目页为16 token，Strata为4；一个QSA组命中可能
带入四倍页内数据，需考虑访问放大。完整 host 副本＋device slots 在 Thor
不自动省总 DRAM；当前没有充足理由仅为单流200K照搬这一双份存储方案。

### 2. 后缀草稿与按实际整轮成本决策

[suffix_drafter.cpp:33-85](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/spec/suffix_drafter.cpp#L33-L85)
用 token trigram hash 查历史，再逐 token 确认最长后缀匹配，提出历史续文。
[draft_policy.cpp:60-107](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/spec/draft_policy.cpp#L60-L107)
以期望接受 token 数/实测 round 成本选择 MTP 或 lookup 及长度，EMA 更新成本；
还会重新探测长期未测的尺寸，避免一次慢轮永久封死候选。
这确已接入 [生成循环](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/program/generate.cpp#L12564-L12605)，
`:12750-12755` 将接受数和整轮耗时回灌，所有草稿仍交 target verify。

**增量：** 面向代码复制、重复文档可能省草稿模型工作，或延长有效 verify 窗口。
不应照抄5090/CPU成本先验。时间驱动选择 T 迁入本项目后可能导致不同计算形状、
近 tie 与重复输出变化，这是风险推断，不是已证明 Strata 输出不确定。
如研究，先固定 verify T/确定性策略，区分词法复用收益和原有 MTP 收益。

### 3. 少复制的所有权交换与完整会话恢复

[ExchangeStorage::commit](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/include/strata/core/exchange_storage.hpp#L78-L94)
在 CPU reader/H2D 已结束、被换出数据已落备用缓冲后，通过 slot ID 交换所有权，
不再 host memcpy。`expert_source.cpp:3371-3399` 实际调用它；失败拒绝发布错误
驻留关系。适合参考本项目可选 MoE tiered 路径的 staging/L2/mirror 生命周期，
但它不能消除必要 H2D/D2H，也不能给当前全驻留基线带来换页收益。
`expert_cache.cpp:638-655` 的基础 admit 本身无淘汰，动态交换由上层负责，
不要据单个函数误判整个引擎只有静态缓存。

[conversation_snapshot.cpp:147-225](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/core/conversation_snapshot.cpp#L147-L225)
只复用完整未改页，重拷 partial page 与 indexer moving row，校验 codec/geometry；
恢复 host 后清空旧 residency，draft ring 重填。这些同模型的边界比简单
“支持 prefix cache”更值得参考。本项目若增加会话复用，仍须同时覆盖
GDN、PLE conv、QSA raw/comp、trunk、draft、token/ngram history。

**不采用为透明优化：**
[route_prior.cu:30-68](https://github.com/Niko1221/Strata/blob/296b59d2f73c8f04a805df29c2fa97655adace78/src/kernels/cuda/route_prior.cu#L30-L68)
给驻留专家 logit 加 lambda，改变 top10，随后用原 logit 对选中专家重归一。
这是显式改变路由，不能因命中率/任务分数好就纳入现有计算合同。
同样，CPU AVX 专家路径不是 Thor ARM 的可直接移植加速手段。

## KVMem：重点借事务与失败边界，不照搬其检索

### 原生 payload 与 prepare → spill → restore → publish

[memory_contract.hpp:27-129](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/include/kvmem/memory_contract.hpp#L27-L129)
区分 backend/model/schema、session incarnation、内容/统计/view版本、
committed frontier；原位置与 compact 地址分离，K/V与scale分别描述。
[memory_contract.cpp:213-337](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/src/host/memory_contract.cpp#L213-L337)
核查 stamp，完成 spill/restore 后才 publish；abort 必须 drain，旧view
无法恢复时明确 invalid。

[HostKvStorage:97-187](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/src/host/host_kv_storage.cpp#L97-L187)
以 lease pin 住数据，只驱逐 sealed 且无人使用的记录；最多256KiB合并写缓冲
也计入预算；全部写成功前原副本保持有效且继续计费。冷读可直接进入调用者
staging，不必把全记录提升到 RAM。
但 [llama NativeTransfer](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/src/adapter/llama-memory-kvmem.cpp#L5751-L5762)
明确当前复制路径返回前 fence，不宣称异步 transfer。接口支持异步不等于当前
生产路径实现计算/I/O完全重叠。本项目已有注册buffer/io_uring PLE reader，
没有依据用其 pread/pwrite 层替换后便宣称更快。

### 会话策略也要恢复，拒绝 draft 不得污染统计

[commit_speculative](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/src/adapter/llama-memory-kvmem.cpp#L4008-L4068)
校验 execution history/frontier，只合并 n_keep 个已接受 token 的 mean-K。
草稿 follower 共享物理 slot 排布，却保留独立 KV codec/storage；该适配器
明确只支持单序列，不能替代本项目多序列状态设计。

可参考的具体测试：
[spill/restore/publish 故障与取消](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/tests/memory_contract_test.cpp#L206-L262)、
[RLIMIT_FSIZE 真实写失败保留源数据](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/tests/tiered_kv_storage_test.cpp#L297-L317)、
[GDN rejected-tail NaN 与 FP64 对照](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/tests/gdn-replay-test.cpp#L142-L190)。
其 llama patch 也有真实 Record/Fold kernel，但严格限定 Qwen35-27B/单序列/
特定shape，auto仍走snapshot；不能误称现成本项目实现。

### 检索与 QSA 不同

[mean_key_retrieval.cpp:29-82](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/src/host/mean_key_retrieval.cpp#L29-L82)
将 query 沿token聚合，与 pre-RoPE mean-K 比较；mean模式先聚合GQA组内Q
向量再算cosine，max模式取各Q-head cosine最大值，再跨KV heads/层平均，
得到全局块分数；随后 sink/recent/mandatory＋top-k，按原时间排序。
它不是本项目每层每 query 的学习型 QSA：独立投影、RoPE后点积、ReLU求和、
top512组及因果尾部。按该分数先删除 QSA 可能选的块，会改变计算合同。
较安全的未来用法是只预测驻留/预取，实际 miss 仍补齐 QSA 原选全部页。

## ActKV：必须区分论文、社区集成与独立复刻

[原论文](https://arxiv.org/html/2609.31395v1)的对象是多轮 agent，
核心为 action 区域注意力驱动保留、confidence预算、page-aware压实。
本轮未找到作者官方源码入口；不能将此表述为绝对不存在。
KVMem README 归功的是 `melis2023/kvmem-actkv` 的 LRFU/GQA max。

### 社区集成实际做了什么

[adapter:4211-4314](https://github.com/melis2023/kvmem-actkv/blob/fe4c5d9d57f6714f2ec1d6657d237813d7ec1a89/src/adapter/llama-memory-kvmem.cpp#L4211-L4314)
用当前用户 query 聚合 Q 与块 mean-K 做 cosine、GQA max、跨层平均；
不是从生成 action 的精确 attention/LSE 恢复逐 token 概率。
[kvmem_store.cpp:254-302](https://github.com/melis2023/kvmem-actkv/blob/fe4c5d9d57f6714f2ec1d6657d237813d7ec1a89/kvmem/src/host/kvmem_store.cpp#L254-L302)
按**块数量** top-fraction 定 hit，再 `history=lambda*history+score(hit)`。
因此 top-frac=.9 既不表示保留90% KV，也不表示保留90%注意力概率质量。

fork 的 confidence 模块确有
[host logits 入口](https://github.com/melis2023/kvmem-actkv/blob/fe4c5d9d57f6714f2ec1d6657d237813d7ec1a89/src/adapter/llama-memory-kvmem.cpp#L5053-L5082)，
计算分布统计趋势后只增预算；但
[初始化](https://github.com/melis2023/kvmem-actkv/blob/fe4c5d9d57f6714f2ec1d6657d237813d7ec1a89/src/adapter/llama-memory-kvmem.cpp#L923-L954)
须显式令初值低于ceiling，否则增长无作用，GPU pool仍按ceiling预分配。
这是 selection budget 调整，不是动态释放显存。KVMem 当前主仓未移植
该 confidence 模块，不能把整个论文系统算作当前组合的已有功能。

### 最值得借鉴的反而是策略状态事务

主仓 [lrfu.hpp:43-99](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/include/kvmem/lrfu.hpp#L43-L99)
将 begin_round、preview、commit、checkpoint/restore 分开。
preview 总从 committed history 派生；重复probe不重复奖励，取消/重新生成
恢复相应历史。新增缓存热度也应随会话回滚，不能只恢复 K/V。
[对应测试](https://github.com/kvmem/kvmem/blob/0af10ece3d78edb905c62357e52a62147df69d58/kvmem/tests/kvmem_store_test.cpp#L1133-L1179)
检查重复probe、rollback和序列化。这部分与KVMem价值重叠，不重复计功。

### actkv-open 的 kernel 存在，但实际接入有限

[LSE kernel](https://github.com/Maurya30/actkv-open/blob/0345bd76123f78b0c6ce525a28db8cb7d4e271dd/actkv/kernels/lse_attn.py#L86-L158)
通过page table取K，以 `exp(qk*scale-LSE)` 恢复概率；
[压实planner](https://github.com/Maurya30/actkv-open/blob/0345bd76123f78b0c6ce525a28db8cb7d4e271dd/actkv/kernels/compaction.py#L44-L105)
构造互不重叠src/dst，实现并行原位搬移。
但 [HF实际路径](https://github.com/Maurya30/actkv-open/blob/0345bd76123f78b0c6ce525a28db8cb7d4e271dd/actkv/hf/session.py#L155-L199)
仍是 eager attention 与 index_select 新连续缓存，没有生产paged scheduler接入。
其tiny随机模型E2E测试不构成真实agent质量证据。

**本项目判断：** 原生QSA已稀疏，GDN也没有逐token历史KV可删。
额外LRFU淘汰属于新近似模式，不适用现有N=4近tie豁免；对未选KV用选中集合的
LSE重建也不是原模型attention。即便只搬页，也要保留原逻辑位置、group边界与
展开顺序，不能用实数下的置换等价替代浮点逐位合同。

## 建议顺序、移植成本与判停条件

| 顺序/用途 | 具体候选 | 工程成本 | 需要先回答的问题 |
|---|---|---|---|
| 当前decode研究 A | NInfer GPU accept/元数据＋有界子图 | 高：接口、PLE事件边界、调度接线 | 可消除的host gap是否足够；取消/尾部发布是否完整 |
| 当前decode研究 B | NInfer/KVMem GDN Record/Fold | 中高：新kernel与状态提交模式 | checkpoint写入收益是否抵过fold重算；全部接受前缀逐位一致 |
| 当前decode研究 C | 独立草稿head；之后再考虑Strata suffix | 中：派生权重、映射、草稿来源 | 草稿成本下降后接受率/E2E是否仍净赚；确定性是否保持 |
| 多会话/容量需求出现时 | KVMem事务＋Strata真实QSA页驻留/会话恢复 | 高：新的存储生命周期 | 总DRAM是否真下降；miss字节、TTFT与PLE I/O竞争是否可承受 |
| 独立近似研究 | KV低比特、ActKV eviction、route prior | 高：新数值/质量合同 | 需求是否允许改变主模型计算；不得冒充透明优化 |

前三项是可择一冻结范围的候选，**不是同时开工清单，也不承诺+5%或更大收益**。
先消除CPU/GPU边界还是先做Record/Fold，应由现有证据与首轮HTTP结果决定，
不能只按源码看起来简洁判断。后续性能实现仍以tools/evalscope HTTP E2E为
第一项测试，通过后再细分分析；五档、MTP off/on、数值/质量分别验收，
不为本次文档整理重跑现有已通过且身份未变的证据。

## 许可与本轮交付边界

读取固定版本根文件：NInfer Apache-2.0，Strata MIT，melis ActKV fork
Apache-2.0，actkv-open MIT。本轮不引入其代码或模型产物。
KVMem主仓tracked tree未找到LICENSE/COPYING/NOTICE；README仅说backend
保留上游许可，不能自动把这些许可套给新增共享层/adapter。
这是源码包声明的记录，不是法律判断；实际复制前需确认相应文件授权。

本轮完成：身份冻结、关键实现与调用链审阅、本地差异分析、候选排序。
未完成也未声称完成：Thor移植、第三方单测复现、整模型数值/HTTP质量/性能验收。
本项目运行时身份、已接受MTP成果和两候选NO_GO结论均保持。
