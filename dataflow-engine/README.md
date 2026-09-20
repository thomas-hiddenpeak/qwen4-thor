# QTDE — Qwen-Thor Dataflow Engine

模型专用数据流引擎 · 筹备文档 · 2026-09-20

状态：设计推演，尚无实现或新性能实测。QTDE 是暂定工作名。
“数据流机器”保留为设计理念；软件项目称“数据流引擎”。

目标：在 Thor 上运行当前 Qwen3.8-Flash-Next-NVFP4-SSD-Stream
checkpoint，以完整状态转换为优化单位，显式设计数据依赖、生命周期、
物理布局、数值语义和资源预算。kernel 边界由这些约束推导。

确立的采纳原则：**保持当前精度、不造成性能回退时，降低系统复杂度
本身就是有效收益，不要求同时获得速度提升。** 此原则同时适用于当前
runner 和新引擎；具体证据要求见 [验证标准](VALIDATION.md#采纳原则复杂度降低是独立收益)。

## 阅读顺序

| 文件 | 解决的问题 |
|---|---|
| [MODEL_CONTRACT.md](MODEL_CONTRACT.md) | 模型究竟要求哪些操作、状态和顺序 |
| [EXECUTION_PLANS.md](EXECUTION_PLANS.md) | decode / 分块 prefill / 多序列 MTP 如何执行 |
| [RESOURCE_BUDGET.md](RESOURCE_BUDGET.md) | 容量、流量、并行度与临界路径如何记账 |
| [VALIDATION.md](VALIDATION.md) | 如何证明设计正确、有收益，如何选择第一项实验 |
| [STATUS.md](STATUS.md) | 本项目筹备进度、暂定决策与待决问题 |
| [log/2026-09-20.md](log/2026-09-20.md) | 初次推演记录 |

## 边界

- 用户授权在独立目录保存新项目筹备文档。本轮写入限于本目录；状态和
  日志也在此维护，以隔离其他人正在修改的 runner 文档及代码。
- 模型目录、reference/ 继续只读。本轮不编译、不运行 GPU 工作负载。
- 首个执行合同以文本、greedy 为范围；保留视觉 embedding 和三行
  MRoPE 的输入接口。视觉编码器调度、一般随机采样另立合同。
- 使用 checkpoint 的 NVFP4 routed weights、BF16 dense weights /
  activations、FP32 recurrent state 作为首个数值基线。
  FP8 权重 shadow、FP8 residual、索引复用均是独立实验开关。
- 当前 runner 是对照之一，不能以它的注释或历史吞吐作为证明。
- 新架构可以阶段性更慢，但每阶段必须说明增加了什么成本、消除了什么
  约束、下一步有什么可测的改进空间。没有预先承诺的加速倍数。

## 证据等级

- **事实 F**：PDF、checkpoint 配置、当前代码实际表达的结构。
- **推导 D**：由明确形状和假设计算出的容量或逻辑流量。
- **提案 P**：本文选择的候选执行合同，还没有实现。
- **待测 H**：吞吐、驻留、重叠、精度等需要实验回答的假设。

模型语义裁决要联合 checkpoint、匹配的参考实现和差分结果。
报告解释设计意图；报告文字和当前代码都不能单独裁决不一致。
性能裁决需要工作负载、数值模式和硬件状态匹配的实测。

## 来源与时间锚点

主要来源：

- [技术报告](../reference/pdf/tech_report.pdf)：
  *On the Design of Qwen3.8-Next Architecture: Evaluation, Efficiency,
  and Training Stability*，2026-08-26，28 页。重点 §2.1–§2.3。
- [checkpoint config.json](../../llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/config.json)。
- 当前代码：`src/model/`、`src/quant/`、`src/ple/`、`src/mtp/`。
- [SGLang 参考](../reference/sglang-qwen4-exp/qwen4_exp.py)：
  `_compute_qsa_topk_indices` 中的 MTP capture/reuse。
- [tokenspeed 参考](../reference/tokenspeed/python/tokenspeed/runtime/models/qwen4_exp.py)：
  `mix_inject_proj` 合并输入投影映射。

读取时 HEAD：`a77b454e794f9d5d837995cdd8000626da45aec9`。
工作区有并行修改，因此 HEAD 不等于被分析的完整源码快照。
以下 SHA-256 是 2026-09-20 读取时的锚点，不会随源码自动更新：

| 来源 | SHA-256 |
|---|---|
| 技术报告 | `04f263446d74a35cb7cea368574e0c561f3b05c133be2c777ac884404063655d` |
| checkpoint config | `e765305daba0951974308f4d32c075b52a6a45974730d273f2216718a994d624` |
| `src/model/hyperconnection.cu` | `c82111ad82cccec3c729c324b873dfe5a946345046b4fd137dcba8ef3d72e98d` |
| `src/model/linear_attention.cu` | `b965a21df097778e6af722c8a1a03f029135b6ba49e15090a1eb13cdadb85266` |
| `src/model/full_attention.cu` | `0989614e5e00de8e4bb2d162a778473aeac82c7b4e1e44b90a1f2df484f9e8cf` |
| `src/quant/moe_gemm.cu` | `a5709f7e6e6f12bc87e5f93514c6b0eb2d7280346041b3ea843f6426f8a8fe71` |
| `src/mtp/mtp.cu` | `260ba866d7dc4cf207e79b934ace370046f83655742ffa8f9bb4cc114665263a` |

本目录没有采纳其他并行任务的性能结论为已验证事实。
