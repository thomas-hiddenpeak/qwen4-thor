# 模型合同：数据、状态和不变量

2026-09-20 · F/D/P/H 定义见 [README](README.md)。

## 1. 固定形状（F）

| 项目 | 当前 checkpoint / 实现形状 |
|---|---|
| 主干 | 48 层，36 GDN + 12 QSA；每四层前三层 GDN、第四层 QSA |
| 窄流 | hidden size 2560 |
| GR | 4 分支，宽流 10240，低秩 320；每层 attention、MoE 各一个 |
| GDN | 16 key heads，48 value heads，key/value dim 128；conv kernel 4 |
| QSA core | 24 query heads，2 KV heads，head dim 256；每 KV head 对应 12 Q heads |
| QSA indexer | 4 query heads，1 key head，dim 128；压缩比 4 |
| QSA 预算 | 512 个完整微块 = 2048 token，另加当前未完成块尾部 |
| MoE | 512 experts，top-10，intermediate 640；shared expert intermediate 640 |
| 词表 | 248320；输入 embedding 与输出 head 不绑权 |
| PLE | 第 2 层（0-based layer 1），2/3-gram 各 8 heads |
| PLE 行 | 每 token 16 次行查询，行宽 160 FP8 bytes，拼接宽度 2560 |
| MTP | 当前配置一个 QSA draft 层；使用主模型 trunk 与 shifted token |

来源：[模型配置](../../llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/config.json)、
[层加载](../src/model/decoder_layer.cu)、[主模型配置](../include/q4t/model/model.h)。
报告没有完整描述当前 NVFP4 的量化布局，不能仅凭报告重建权重格式。

## 2. 基本对象（P）

| 对象 | 最少字段 / 所有权 |
|---|---|
| TokenSpan | sequence_id、generation、起始绝对位置、长度、token、MRoPE |
| ResidualFrame | TokenSpan、子层版本、[M,4,2560]、dtype；单一写者 |
| GRFrame | 子层输入 [M,2560]、写门 [M,4]、原残差引用 |
| StateHandle | layer、sequence、generation、committed_length、epoch |
| QsaMicroblock | layer、sequence、block_id、原始 KV 地址、压缩 key、完成状态 |
| Selection | layer、sequence、query position、epoch、block IDs、tail |
| ExpertWork | layer、expert、输入 row IDs、数量、量化参数、完成计数 |
| NgramRequest | token span、epoch、hash 行 ID、目标槽、ready event |
| SpeculationEpoch | 起始 committed_length、候选、验证结果、状态日志、提交状态 |
| PlanArena | 已预留容量、对齐、子区间、最后消费者、完成事件 |

generation 防止回收的 sequence slot 被旧 I/O 完成事件误写；epoch
防止被拒绝的推测结果重新变为可见。不同层的同名状态不能互相别名。
这些是逻辑对象，不要求一对象对应一次内存分配或一次 kernel launch。

## 3. 位置约定（P，三份计划共同使用）

- `L` 是已经被主模型消费的 token 数；可见状态对应 `[0,L)`。
- 下一次输入位于位置 `L`。绝对逻辑位置与三行 MRoPE 分开存放。
- `pending_token` 是已选出、尚未进入主模型状态的 token。
- 已发给用户的 token 数和 committed_length 不是同一个计数器。
- MTP draft 使用 EAGLE 式对齐：主干 `h_i` 配输入 `x_(i+1)`，其
  draft 位置是 `i`，预测 `x_(i+2)`；不能把 draft 的长度直接当主模型长度。
- generation/epoch/长度的发布发生在对应 CUDA event 完成之后。

## 4. GR 的实际依赖（F → P）

报告 p11 Eq.30–34，代码 [hyperconnection.cu](../src/model/hyperconnection.cu)。

```text
R [M,4,2560]
    → 每分支归一化 Rhat
    ├→ down [M,320] → SiLU(down/4) → up [M,10240]
    │       → sigmoid + 与 Rhat 相乘 + 分支平均 → x [M,2560]
    └→ inject [M,4] → 2*sigmoid(inject/4) → s [M,4]

y = Sublayer(x)
R'[i] = R[i] + s[i]*y
```

写门不依赖 y，计划将 down 和 inject 的权重按输出维拼接为 324 行，
读取同一 Rhat。读取阶段结束后仅保留 x、s 和原 R；不为后续写门保存
完整 Rhat。融合需保持基线中的中间 BF16 舍入点，或单独申报数值变更。

四分支均保留；不能根据报告的分支分析静态删除“弱分支”。
GRWrite→下一次 GRRead 可以合并数据遍历，但完整分支 RMS 归约与
低秩投影仍构成依赖边界。先规定生命周期，再选择融合程度。
GR 用 (1+w) 的 zero-centered gain；GDN output norm 等具体权重约定
由 checkpoint/参考实现核验，不能把报告的概括统一套到所有 norm。

## 5. GDN 状态（F）

每 head：`S [128,128] FP32`；更新为：

```text
u_t = beta_t * (v_t - alpha_t * transpose(S_prev) * k_t)
S_t = alpha_t * S_prev + outer(k_t, u_t)
y_t = transpose(S_t) * q_t
```

q/k 的卷积、SiLU、L2 norm、query scale、输出 norm 和 sigmoid gate
均属于合同。短卷积保存原投影历史；不能以激活后值替换。
同一层同一 sequence 的状态更新必须满足时间顺序；不同 sequence、
不同 value-head/列 tile 可以并行。块算法改变求和顺序需重新验收。

## 6. QSA 微块与可见性（F → P）

报告 p6–7 Eq.12–19，代码 [full_attention.cu](../src/model/full_attention.cu)。

- index key：先对四个 raw keys 平均，再 norm，再按块起点作 RoPE。
- query 位置 i 只允许选块末端 `4*b+3 <= i` 的完整块。
- tail 从 `4*floor((i+1)/4)` 到 i；长度为 `(i+1)%4`，可为 0。
- Selection 保存最多 512 个块 ID 和 tail，主干 core 保留全部原始 KV。
- 某轮没选中的历史 KV 以后仍可能被选中，不能淘汰为“无用”。
- GPU 可先写一个 chunk 的所有 KV，但读者必须按自己的位置做因果过滤；
  物理已写入不等于逻辑可见。
- 每层、每序列独立选择；初版主模型不复用跨位置/跨层 top-k。
- 压缩表仅需 `ceil(max_len/4)` 容量。边界 raw tail 加当前 chunk
  scratch 即可支撑普通执行；MTP 需额外保存 epoch 起点 tail 和版本。

## 7. MoE / PLE / 数值边界（F → P）

MoE router 的 top-10、softmax/归一化、tie 顺序及 shared gate 都要
从实际参考固定。权重 FP4 payload、group scales、global scales 和
每专家 activation scale 是一个整体。不同专家的 input scale 可能
不同，不能把所有专家输入先统一量化一次而不证明等价。
combine 按原 top-k slot 顺序归约，禁止用无序 atomicAdd 当等价替换。

PLE hash 的 EOS 截断规则、head offsets 和表 scale 随模型包固定。
只读查询结果可以缓存；PLE short-conv 是可变状态，必须进入事务。
I/O 失败不能静默补零；请求失败后完成事件仍需排空或 generation 校验。

第一版 greedy 合同要求确定性 tie 规则；融合首先保持参考精度边界。
若改精度或运算顺序，记录独立模式，测 logits、路由、长程状态及任务质量。
“argmax 大多相同”不能证明任意长生成严格等价。
