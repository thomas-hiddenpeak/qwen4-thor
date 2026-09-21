# Serve logits：按消费者行数分配

2026-09-21。直接父版 `00bddff`，固定性能参考仍为 `fcb5925`。
证据 `.q4t-work/e2e/serve-logits-capacity-v2-20260921/`。
当前：HTTP、数值、预算、实际分配与执行结构全部通过，最终审查接受。

## 实现与边界

共享 `d_prefill_logits_` 从 `[max_prefill,vocab]` 改为
`[max_seq,vocab]`。所有读写受 `model_mu_` 保护，仍由 serve 持有。

| 消费者 | 写入与读回 |
|---|---|
| 单序列短 prefill 调度 | 保持已接受的一行模式，读行 0 |
| B>1 短 prefill 调度 | 保持已接受的每序列末行，按打包顺序读 B 行 |
| 长文本 chunk 调度 | 保持已接受的仅末块一行模式 |
| 内联短请求、图像、视频 | 显式选择末行 head，读行 0 |
| 内联长请求 | 取消首块无消费者的 head，仅末块计算一行 |
| MTP 主模型 prefill | 同上，但独立完整 trunk 仍逐块写满供 draft-extend |
| 调度器不可用时普通 decode | 保持一行输出 |

MTP draft/verify 和 scheduler decode 有独立 logits，不缩减这些缓冲。
非 serve API 默认全行语义保持。模型 checkpoint、精度及视觉处理不变。
没有引入扩容、特殊全行回退缓冲或新同步。

预算器删除固定 `max_prefill*vocab*2` 项，在每序列状态池计入
两份词表行及一个 int32 argmax token，取代旧 scheduler 500000 字节
估计。其余 workspace/MTP/状态估计保持，不宣称整个预算精确。

## HTTP 先行验收

零警告构建后先质量 11/11，再完整五档 15/15，均输出一致、服务
正常退出；直接父版与固定参考无不利范围分离。MTP 默认关闭，
单流 greedy，五档输入 1024/4096/8192/45056/204800，各三次输出 256。

| 输入 | TTFT 秒 | decode tok/s |
|---|---:|---:|
| 1024 | 0.800 | 18.586 |
| 4096 | 2.634 | 17.858 |
| 8192 | 5.168 | 18.098 |
| 45056 | 30.470 | 17.856 |
| 204800 | 159.486 | 17.105 |

TTFT 含 HTTP/分词/prefill；decode 按首 token 后总 token 数/总时间。
这些是原完整矩阵，未替换最优样本，不宣称普通 decode 提速。

随后完成真实 B=2 九轮 27 请求、长短交错九轮 18 请求、四项生命周期，
再完成内联 18 请求、回退 18 请求、视觉 36 请求、MTP 27 请求。
全部输出对照通过。生命周期的复制/同步错误和 scheduler 分配失败
是合成故障，不是真实硬件故障或 OOM 证明。

两次性能门禁异常均保留，未用低层测试覆盖：

- 回退 44K decode 首次相对前侧父版不利分离、与后侧重叠。
  同配置三侧复核另 18 请求，候选 17.0389–17.0816 tok/s，
  两侧父版 17.0108–17.0900 / 17.0383–17.0586，未重现。
- MTP 8K decode 首次相对后侧父版不利分离、与前侧重叠。
  仅受影响档位三侧复核另 9 请求，候选 27.5777–27.6777，
  两侧父版 27.5204–27.5404 / 27.4134–27.5086，未重现。
  不把此次有利分离当成稳定提速。

联合判断见 `fallback-joint-review.json`、`mtp-joint-review.json`；
原失败 review 保持未通过。全部消费者 HTTP 后才运行以下低层验证。
视觉生成请求使用显式模型模板，另保留原始单图立即停止行为。
这不证明标准 messages 的模板接入问题已解决，也不是全面视觉质量保证。

## 数值证据

- 文本 1K/8K/44K，复现实际旧首末块 head / 新仅末块末行策略。
  555745280 个完整 trunk BF16 值及三组固定 token 续算逐位一致，
  canary、序列位置/历史通过；覆盖 MTP 所需完整 trunk，而非仅最后行。
- 实际 PNG→processor→27 层视觉塔→多模态展开/MRoPE→完整主模型，
  单图/双图/视频 3317760 个 trunk 值、MRoPE 表/偏移、三组固定续算
  逐位一致，输出 canary 通过；输入 token 数与 HTTP 一致。
- 文本/视觉各三组 logits 有非逐位差异，最大相对 L2 误差分别
  0.000358 / 0.000922，最大绝对差均 0.03125。不称为“一 ULP”。
- 从实际 head 捕获相同 normed，读取同一 BF16 权重做 CPU FP64
  投影及原 BF16 边界舍入：六组末行 mixed 均匹配参考，六组最终
  logits 对共同参考的相对误差均低于旧全行，argmax 与参考一致。
  接受已测样本精度，非全输入逐位等价保证。

详见 `text-numerical/`、`text-reference/`、`text-precision-review.json`、
`vision-numerical/`、`vision-reference/`、`vision-precision-review.json`。

## 容量与执行结构

S=1 的实际 `cudaMalloc` 请求及 `cuMemGetAddressRange` 返回区间均为
4068474880→496640 字节，调用位置解析为 `ChatServer::Start`。
差额 4067978240 字节，约 3.789 GiB。**不是整机驻留/峰值减少量。**
S=2/4 的 scheduler 与 prefill logits 各自分配 993280/1986560
字节，CUDA 地址区间与请求相等；两次真实 HTTP 输出一致、服务
正常退出。并发正确性另由 B=2 与生命周期门禁覆盖。

预算旧/新实际源码共 64 配置（MTP 开关、prefill 1024/8192、
max_seq 1/2/4/8、固定/自动长度）核对通过，自动长度与独立算术一致。
采用 serve 的 84 GB 权重预算输入；旧 workspace 估计准确性不在本轮结论内。

父候选内联 44K、单图、视频、MTP 44K 共八份真实 evalscope HTTP
时间线，输出与接受记录一致，六次服务正常退出：

- 内联/MTP：首块六个 head kernel 删除，末块六个 head 改为单行。
- 单图/视频：主模型 head 六 kernel 从 84 行改为 1 行。
- 其余各 stream kernel 名称/grid/block/顺序一致。

初始局部 diff 被相邻 forward 的同名 mixer 干扰，原文件保留；
正式比较用 EmbedLookup 划分 forward。记录见
`consumer-timelines/structure-review.json`。不据 kernel 签名相同
推导指针/标量参数相等，数值证据独立验证状态。插桩时间不作性能验收。

## 历史与剩余项

旧候选 `serve-logits-capacity-20260921/` 基于 5d560f5，视觉 HTTP
暴露父版竞态后停止；运行时补丁与失败记录保留，未接受。
视觉 barrier 已作为 00bddff 独立接受后，本轮从新基线完整重验。

最终源码、二进制、库及证据摘要已复核，`final-audit.json` 接受；
固定参考不更新。按保持精度/性能并减少容量和索引复杂度接受本轮。
