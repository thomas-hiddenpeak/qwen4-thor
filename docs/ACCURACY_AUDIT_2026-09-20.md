# QSA 精度排障与验收（2026-09-20）

本轮发现的是实现错误，不能继续用“NVFP4 near-tie 噪声”解释。
固定标准答案 HTTP 检索题从旧版 **6/11** 恢复到 **11/11**，覆盖实际输入
1024、4096、8192、45056、204800 token。五档各三次重复输出一致，但 decode
相对旧版低 0.36%–1.73%，**性能无回退条件未通过**；这套检索题也不等同于
全面模型精度基准。本轮不标记整体优化完成。

## 请求与判据

使用模型 tokenizer 的原生 chat template，enable_thinking=false，模板渲染
后通过 prompt 字段发往真实 /v1/chat/completions。单并发、greedy、MTP 关闭，
输出上限 32，SSE usage 使用服务端计数。max_prefill=8192，max_len=208896。
输入是编号日志中的唯一权威六位审计码，要求仅输出该码；对 text.strip()
做精确字符串匹配，不做模糊评分。1K/4K/8K 各测约 10%/50%/90% 三个深度，
44K/200K 各测中间位置。没有使用随机 token 承担语义验收。

所有运行使用 tools/evalscope 环境中的 perf 客户端；自有脚本仅准备请求、
管理 serve 生命周期、读取其数据库并核对明确答案。每个运行时改动都是
必要构建后先做 HTTP E2E，没有单测、bench、profile 或数值分析前置。

## 分步证据

| 阶段 | 题目 | 精确正确 | 说明 |
|---|---:|---:|---|
| 旧二进制 | 11 | 6 | 三条 8K 以及 44K、200K 错误 |
| 固定索引顺序、并行整数归约 | 11 | 7 | 仅 8K 中间题额外恢复 |
| 单独补索引器跨 warp 同步 | 9 | 7 | 未解释两个短档错答 |
| 再补 RMSNorm 的 1+weight | 9 | 9 | 两条 8K 错答立即恢复 |
| 再修 RoPE 方向 | 9 | 9 | 单独首测通过 |
| 再修 QSA 未完成组尾部 | 9 | 9 | 单独首测通过 |
| 再读取生成停止 token 列表 | 11 | 11 | 所有请求正常 stop，实际长度匹配 |

长档未在每个中间版本单独执行，不能把 44K/200K 的改善单独归因于某一项。
完整修复后每个答案都是六位码，无下一轮角色文本，每条 usage 为 7 个输出
token（含停止 token）。服务正常退出 0。

8K 三题还分别用 max_prefill=4096/2048/1024 重放，与 8192 块大小逐字相同，
均为 1/3 正确。因此现有失败证据不支持“大块 prefill 是根因”。

## 代码原因与修复

1. **索引器 RMSNorm 漏加 1**。原始 checkpoint 权重直接加载，未做 +1
   折入；IndexerNormRopeKernel 和 BuildCompressedKKernel 却按普通 RMSNorm
   使用 weight。官方 Qwen4ExpTextRMSNorm 和 tokenspeed GemmaRMSNorm 均采用
   1+weight。查询及压缩键两个分支都已修复。
2. **RoPE 旋转方向相反**。官方 rotate_half([a,b])=[-b,a]，应为
   (a*cos-b*sin, a*sin+b*cos)。主注意力、索引查询、压缩键已统一修正。
   旧 CPU oracle 照抄了错误符号，短 T=8 测试还退化为 dense attention，
   因而既没有独立验证旋转，也没有观察索引选择错误。
3. **跨 warp 读取缺同步**。索引器原地归一化后读取另一 warp 的旋转伙伴，
   已在两个阶段间加入 block 同步。单独修复未改变本轮错答，不夸大其因果性。
4. **QSA 额外强制加入完整当前组**。参考只附加 (pos+1)%4 个未完成组 token；
   完整组由 top-k 决定。三条展开路径已对齐，删除当前组成员扫描，同时保留
   固定槽位顺序，避免原 atomicAdd 分配导致注意力归约顺序不确定。
5. **生成结束符不完整**。config.json 主 EOS 是 248044，generation_config.json
   指定 [248046,248044]。serve 现在识别整个生成停止列表；PLE 的历史填充
   继续使用原主 EOS，避免改变 n-gram 语义。缺失可选生成配置时回退主 EOS，
   非法列表拒绝启动。

本地参考来源（只读）：

- .q4t-work/refenv/lib/python3.12/site-packages/transformers/models/qwen4_exp/modeling_qwen4_exp.py：
  Qwen4ExpTextRMSNorm、rotate_half、Qwen4ExpTextQSAIndexer。
- reference/tokenspeed/python/tokenspeed/runtime/layers/layernorm.py：GemmaRMSNorm
  的 weight+1 处理。加载器未预处理的结论来自本项目 LoadFullAttention。
- 模型只读目录内 config.json 与 generation_config.json。

## 性能与数值验收状态

每档取初始基线的第一条输入，旧、新版本分别连续重复三次，输出 256。
双方均 15/15 HTTP 成功、实际长度达标、finish_reason=length、服务退出 0。
修复版每档三次生成文本一致。计时期间没有并行编译或其他推理任务。

| 实际输入 | 旧 TTFT 秒 | 新 TTFT 秒 | 旧 decode tok/s | 新 decode tok/s | decode 差值 |
|---:|---:|---:|---:|---:|---:|
| 1024 | 0.879 | 0.885 | 15.564 | 15.507 | -0.36% |
| 4096 | 3.233 | 3.243 | 14.238 | 13.992 | -1.73% |
| 8192 | 6.515 | 6.514 | 14.817 | 14.617 | -1.35% |
| 45056 | 39.731 | 39.802 | 14.435 | 14.308 | -0.88% |
| 204800 | 234.823 | 234.891 | 13.929 | 13.776 | -1.10% |

TTFT 为三次算术均值，不是纯 prefill 时间；decode 用
sum(completion_tokens-1)/sum(latency-first_chunk_latency)，不是逐请求速率
的算术均值。每次原始值及首请求信息均保留在 results.json 与数据库。
4K 旧版范围 14.234–14.241，新版 13.924–14.083；200K 旧版 13.899–13.989，
新版 13.771–13.779。不能把这些差异直接归为组内噪声，接受条件未满足。
三次测量不足以建立尾延迟或证明稳定的微小加速。

旧版存在已确认的数学错误，两版生成内容和专家路由可能不同，速度差异
不能全部解释为修复 kernel 的直接开销；也不能据此豁免无回退要求。
保留正确性修复候选与全部证据，性能差异单独待处理。此前仅固定顺序的
中间版也出现约 1%–2% 回退，本次删除尾部成员扫描并未完全消除它。

旧的“固定 Q/K/V 与选中集合”快照数值方案已停止：本次修正数学语义后，
这些中间量理应改变，继续套用其假设会产生错误结论。CPU oracle 已修正，
并增加缓存原始/压缩索引键检查，避免仅观察短 dense 输出。对应源代码在
tests/model_full_attention_test.cpp；性能门禁未通过，尚未执行该检查。
五档 E2E 结束后仅构建 q4t_tests，编译通过且无警告（build-oracle.log）。
无 bench、profile 或低层数值测试被用于覆盖这一结论。

## 证据位置与限制

全部本地产物位于 .q4t-work/e2e/acceptance-20260920/：

- quality-inputs/：精确请求及答案、深度、prompt 摘要。
- quality-old/、quality-new/、quality-sync-only/、quality-centered-norm/、
  quality-rope-corrected/、quality-qsa-tail/、quality-final/：分步请求结果、
  原始 evalscope 数据库、服务日志、二进制摘要、退出码。
- quality-chunk-{4096,2048,1024}/：失败请求的块大小对照。
- old-matrix/、final-matrix/：同输入性能与重复输出。
- 各阶段源码/二进制快照及 build-*.log 保留；未执行的 capture-e2e/ 和
  new-matrix/ 有 blocked.json，不得将已构建误记为已验证。

旧版二进制 SHA-256 为
16a67b3615db8e2f7635e18f1da82aa49b8bf1c42e01ddc7dd890d0e8496a687，
完整质量与最终性能使用的修复版为
e98969c8d0d08572437745000d2a741a83b97d09600e60cf2ffd746643b3dcd3。
生成语料脚本 prepare-quality.py、重放脚本 run-quality-stage.py 和
run-final-matrix.py 均保存在同一证据目录。各次数据库里的 request 保存
实际 HTTP 请求；不可只依据预期 manifest 判断长度或答案。

本轮不证明 messages 的模板转换、多轮对话、多模态、MTP、并发或全部任务
质量。CLI generate 的停止处理尚未统一。池化与归一化的融合舍入和参考的
分阶段 BF16 舍入仍可能不同，需要独立数值证据，不能一概称为量化噪声。

## 后续候选（仅源码发现，未修改、未归因）

- FullAttentionForward 为获得 max_pos，在判断长短路径之前无条件复制
  positions 到 host 并 cudaStreamSynchronize。它不只发生在长上下文路径；
  模型有 12 个 full-attention 层，普通一步 decode 因而有 12 次此类同步。
  注释关于长路径的说法不能代替真实执行位置。这是后续阶段预算需要核对的
  项目，但本轮未 profile，不能报它的耗时占比或声称它造成上述版本差异。
- d_ik 先复制到 d_ik_raw，再做逐 token norm+RoPE；后续只消费 raw 副本
  构建压缩键，不消费变换后的 d_ik。可考虑删除无消费者变换及配套副本，
  作为符合“性能持平也接受”的复杂度改进候选，仍需独立改动后的 E2E。
