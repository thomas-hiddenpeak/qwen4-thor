# tools/verify/ — 验证标准体系 (Phase 2 完成标准)

C++ NVFP4 W4A4 引擎 vs transformers 5.16.1 参考 (dequantized FP32 权重 +
全精度激活) 的**可重复**正确性验证。固化 Phase 1 的 "L2 噪声保真度" 方法
(见 LOG.md 2026-09-07), 供后续 batch / PD 重构时做回归网。

## 核心认知

两侧差异**就是** NVFP4 量化噪声 (C++ 把激活也量化到 e2m1 4-bit)。判据问
"**差异是否只有量化噪声、有无系统性 bug**", 而非 "logits 是否逐位一致"
(永远不可能)。

## 三判据 (compare_logits.py)

对同一 prompt 的 prefill logits, 逐位置:
- **l2_rel[t]** = ‖L_cpp[t]−L_ref[t]‖ / ‖L_ref[t]‖
- **argmax match** (C++ vs 参考)
- **参考 gap g_t** = L_ref[top1] − L_ref[top2]
- **噪声 delta_t** = (L_cpp[top2]−L_cpp[top1]) − (L_ref[top2]−L_ref[top1])

阈值 `tau = 3 * std(delta)` (对全部位置)。判据:
- **[A] 主判据 (greedy 正确性)**: 每个 CONFIDENT 位置 (参考 gap > tau)
  argmax 必须全对。系统性 bug (错权重 / GEMM / 路由) 会破坏这些位置,
  纯量化噪声不会。
- **[B]** 每个 argmax 翻转都是 near-tie (gap ≤ tau): 参考自身在该位置就在
  噪声水平内, C++ 选不同 token 是预期, 非错误。
- **[C]** l2_rel 在 W4A4 噪声带 (mean ~0.2, max < 0.75)。

退出码: A∧B∧C 全 PASS → 0, 否则 1。

## 用法

```bash
# 完整 48 层基线 (推荐; ~数分钟, CPU 参考 lazy dequant)
python3 tools/verify/verify_logits.py \
    --prompt "The capital of France is ..." --layers 48 --tokens 256 \
    --out /tmp/verify48

# 快速冒烟 (4 层, 含首个 full_attention; 注意 prompt 需足够长才有
# confident 位置, 否则判据 [A] 无法验证 greedy)
python3 tools/verify/verify_logits.py \
    --prompt "..." --layers 4 --tokens 256 --out /tmp/verify4

# 用现成 token 序列 (跳过 encode)
python3 tools/verify/verify_logits.py --ids-file /tmp/prompt.txt --layers 48
```

Pipeline (verify_logits.py 自动编排):
1. encode prompt → token ids (transformers tokenizer, 与 C++ BPE 同源
   tokenizer.json);
2. **C++ dump**: `q4t_tests model_forward_dump_decode`
   (env: `Q4T_MODEL_LAYERS` / `Q4T_DECODE_STEPS=0` / `Q4T_DECODE_PROMPT_FILE`
   / `Q4T_DECODE_OUT`) → `<out>.prefill.bin` (T×vocab f32);
3. **参考 dump**: `ref_dump.py` (逐层 lazy dequant, 支持全 48 层;
   env: `Q4T_REF_LAYERS` / `Q4T_REF_PROMPT_FILE`) → `<out>.logits.npy`;
4. **对比**: `compare_logits.py <cpp>.prefill.bin <ref>.logits.npy`。

## 文件

- `verify_logits.py` — 主驱动 (encode + 两侧 dump + 对比 + 退出码)。
- `ref_dump.py` — 参考 forward (transformers qwen4_exp 类 + 真实权重,
  51 GB PLE nn.Embedding 换成 sidecar gather, 路由专家逐层 lazy dequant
  控内存峰值 ~23 GB)。
- `compare_logits.py` — 三判据对比 (自包含, 带退出码)。

## 覆盖范围

- **纯文本 prefill logits**: 本目录 (C++ vs 参考, 全 48 层)。
- **decode 路径**: 见 LOG.md 2026-09-06/07 (E1–E10 实验链, 增量自洽
  0.000173 / 对参考等距; C++ vs 参考 16 步 12/16 argmax, 不匹配均
  near-tie)。
- **多模态**: 走另一条已闭合验证链 — ViT CUDA vs numpy l2_rel (image
  0.0317 / video 0.0206, 纯 BF16 精度) + 3D MRoPE 差分
  (tools/mrope_diff_test.cpp vs mrope_ref.py, 63 坐标逐位一致)。多模态
  的端到端 logits 对比 (含视觉特征注入) 尚未纳入本 harness, 属后续扩展。

## 校准基线 (2026-09-12, 完整 48 层)

prompt = "The capital of France is a city known for its art, history, and the
river Seine that flows through it. ..." (79 tokens), `--layers 48`:

```
argmax match: 71/79 (89.9%)
l2_rel: mean 0.1402  max 0.2800  p99 0.2258
noise delta: std=0.5522 -> tau=3sigma=1.6567
[A] CONFIDENT-position argmax: 44/44 PASS   (主判据)
[B] 8 mismatches, all near-tie (max gap 0.368 <= tau 1.657): PASS
[C] l2_rel band: mean 0.140 (48-layer ~0.14), max 0.280 (<0.75): PASS
OVERALL: PASS
```

判读: 44.3% 位置是参考 near-tie (gap<tau), 这些位置选哪个 token 都在噪声内;
C++ 匹配全部 44 个 confident 位置 + 44 个 near-tie 中的 36 个 (≈50% 随机 +
少量), 无系统性错误。差异纯为 NVFP4 量化噪声。

**l2_rel band 与层数相关** (噪声逐层累积): 4 层 ~0.05 / 16 层 ~0.21 /
48 层 ~0.14。`compare_logits.py` 的 [0.10, 0.35] band 按完整模型校准; 短层
smoke 可能低于下限 (此时 [C] 仅供参考, [A]/[B] 才是真信号)。
