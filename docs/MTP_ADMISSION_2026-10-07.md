# 单流文本 MTP 准入（2026-10-07）

**阶段结论：NO_GO_FOR_ENABLEMENT，暂不启用 MTP。**
资源、状态恢复及生成边界的具体缺陷已修复；实际完整模型服务中，
相同输入的 MTP 与普通 greedy 仍产生不同的 256-token 输出。该差异
尚未完成数值接入解释，不据此直接断言 MTP 算法错误，也不以短答案
质量通过代替跨路径准入。本阶段按预设 NO_GO 出口收尾，不追加优化。

唯一工作分支 `codex/mtp-admission-20261007`，起点 main `8cd297b`。
实现提交 `f02e75e`、`0284b60`、`ec0ae6f` 已推送。最终二进制 SHA-256：
`7fe34d6ddd6bc78c4c9dea6537f72802c93fc60f09eb0b977105660480c627c7`。
main、原默认二进制和两处未提交修改保持不变；测试服务已停止。
本阶段不自动合并或部署，MTP 默认仍关闭。

## 冻结范围与完成内容

单 Thor、指定 checkpoint、文本 greedy、S=1、k=3、max_prefill=8192、
max_len=208896。仅修资源、生命周期、生成与验证入口的明确缺陷；
不扩展 offload、媒体、多流、动态 k 或 kernel 性能优化。

- 补齐独立 MTP 权重、workspace、scratch、verify checkpoint、请求
  trunk、临时缓冲及 host staging 预算。加载/扩容失败释放部分资源，
  成功后才发布容量，借用的 main embedding/lm_head 不被释放。
- 修复 scheduler 不可用时回退、上下文尾部普通 decode 收尾、输出
  上限与 EOS 优先级；记录每个真实 HTTP ID 的实际执行路径。
- 普通 scheduler 的 `ModelDecodeBatchMulti` 补写当前 token 的三行
  RoPE 坐标；旧入口读取了 prefill 范围外仍为 0 的表项。
- checkpoint 恢复按实际写入行数定位，区分分配容量与当前布局，
  记录有效槽位并拒绝未写/陈旧状态；覆盖重置、增长及失败清理。
- 扩展现有 evalscope runner 的 MTP 模式与容量/路径审计，保存原始
  HTTP、失败与身份，不把静默回退或缩容计为 MTP 通过。

兼容限制：旧 `ModelDecodeBatch(save_checkpoints=true)` 的单序列
PLE kernel 没有完整保存 PLE checkpoint，现于 GPU 提交前明确拒绝，
提示使用 `ModelVerifyMulti`。非 serve CLI 仍调用旧 `MtpSpeculativeStep`，
会遇到该错误；本轮未迁移此入口，不宣称所有 MTP 入口可用。

## 缺陷反例与定向修复

| 问题 | 修复前实测 | 修复后 |
|---|---|---|
| pooled decode RoPE | position=13/delta=0、position=14/delta=5 的三行均为 0 | 分别为 13、19；其他表项不变，真实四层前向完成 |
| checkpoint 容量/布局 | cap1 与 cap3 的相同 prefill/verify 逐字节相同，但恢复后的 layer 1 SSM、conv 及后续 logits 不同 | 恢复状态及后续 probe 逐字节相同，未写行/无效槽/失效状态均拒绝 |
| MTP 权重预算接入 | index 声明 14,734,642,001 B，导致 208896→129753 缩容，正式审计在 HTTP 前拒绝 | 31 个映射张量实际合计 5,214,301,696 B，全容量保持 |

只读 shard JSON 头与加载代码的独立核对确认：当前 BF16、FP8 shadows
关闭时，31 张量合计等于实际独立权重分配，没有借用权重的重复计入。
预算 adapter 改为映射张量尺寸求和并检查累计溢出，不修改模型或预算
比例。最大 host staging 仍为 3,355,443,200 B。全容量总分配估算为
113,469,410,520 B；估算不等于物理 RAM 上限，也不扩展到其他格式。

本机 Release/C++23/CUDA 13.3/SM110a 构建零警告。22 项预算、2 项
生成策略、18 项 runner 模式合同通过。真实 CUDA 生命周期覆盖
22 个 scratch 分配失败点、3 个 checkpoint 分配失败点、部分加载
两种 stream、真实嵌套加载失败/重试及借用权重保护；最终受影响的
有效性检查亦通过。

## 尚未闭合的数值与生成差异

四层实际 B=1 plain/Multi 对照保留两代结果：

| 候选 | 首行 logits 最大绝对差 / 相对 L2 | 结果 |
|---|---|---|
| 修复前 b839a674 | 0.240234375 / 0.0466552478 | numeric_equal=0，rollback_exact=1，greedy_equal=0 |
| 修复后 30736563 | 0.203125 / 0.0400214922 | numeric_equal=0，rollback_exact=1，greedy_equal=0 |

固定后续 token 的四行 argmax 相同，但自然生成首步 correction 仍为
MTP 359 / plain 220。跨路径逐位不同本身不证明算法错误；同路径容量
变化则必须逐位一致。旧 same-path rollback 对照共享恢复逻辑，不能
代替后来增加的独立 cap1/cap3 反例。

唯一一次旧二进制 hook 采集为 492 文件、31,343,616 B。重复 prefill
逐位相同；固定 token 的 layer 0 首个观测差异在 qkv_raw（首行
19/10240 元素），自然首 bonus 更早在 x（4/2560）。这是有界定位，
不是完整误差归因或新容差；hook 改变同步，不用于性能结论。

完整模型的 1K HTTP 控制进一步观察到跨模式差异：

- 普通模式三次确定输出 SHA：`0e1e7da5…`。
- MTP 取消后恢复输出 SHA：`055d47b9…`，1024 输入/256 输出，正常结束。
- 全新服务仅一次正常 MTP 请求仍为 `055d47b9…`，实际 MTP 74 步，
  请求语义、精度和容量相同，服务正常退出、槽位恢复、GPU 健康。

因此该输入未观察到取消后的状态残留；MTP/普通模式的生成分叉在
无取消的新进程也存在。首次取消专项因普通输出比较失败而返回失败
的记录保留，不改写为整体通过。未发明阈值或将所有差异归为 near-tie。

## HTTP 与性能证据

| 检查 | 结果与边界 |
|---|---|
| 普通模式固定质量 | 03 候选 11/11，含 200K；输出参考和实际路径通过 |
| MTP 首次正式启动 | 03 因缩容被拒绝，0 条 HTTP；首次失败保留 |
| MTP 补修后固定质量 | 04 全容量 11/11；逐 ID 均为 Multi B1，各 2 步，无回退 |
| 上下文/故障边界 | 3 组各 4 条，12/12；MTP 后普通收尾、初始尾部、scheduler 分配故障回退，文本与修复后的普通模式一致 |
| 输出上限 | off/on 各 6 条，均通过；1/2/8 token，流式及非流式，文本一致 |
| 取消与恢复 | 取消协议/槽位通过；恢复响应完整，但与普通文本不一致；唯一新进程控制与恢复 MTP 文本一致 |
| 普通模式五档 | 新旧各 15 条；同输入、各自三次输出确定，均为 256 token；五档新旧文本均不同 |
| MTP 五档收益 | 数值/生成准入未闭合，按 NO_GO 出口未运行；不宣称 MTP 加速或性能接受 |

04 相对 03 仅 MTP 预算 adapter 改变，`libq4t_model.a` 逐字节相同；
直接计算/状态合同和普通固定质量按未变执行路径复用，并明确绑定
原受测二进制。04 的普通五档、MTP 质量及边界为实际新运行。

普通模式同输入描述性对照如下。TTFT 为算术均值，decode 为调和均值，
整请求为算术均值；每档 3 次。旧普通路径有已证明的 RoPE 缺陷，因此
旧文本只作兼容对照，不能要求恢复错误计算来维持输出。

| 输入 token | 旧 TTFT(s) | 新 TTFT(s) | 旧 decode(tok/s) | 新 decode(tok/s) | 新整请求(s) |
|---:|---:|---:|---:|---:|---:|
| 1024 | 0.8202 | 0.7989 | 18.5175 | 18.5514 | 14.5445 |
| 4096 | 2.6258 | 2.6307 | 17.8748 | 17.8913 | 16.8835 |
| 8192 | 5.1614 | 5.1723 | 18.1131 | 18.1353 | 19.2332 |
| 45056 | 30.4047 | 30.4499 | 17.8453 | 17.8581 | 44.7291 |
| 204800 | 159.1450 | 159.1545 | 17.1249 | 17.0711 | 174.0920 |

Decode 变化依次 +0.18%、+0.09%、+0.12%、+0.07%、−0.31%。1K 首请求
TTFT 旧 0.9308s、新 0.8703s，后两次均约 0.763–0.767s，不能把首档
均值下降解释成稳定 prefill 加速。逐次值、首请求与后续范围保留。
输出路径已变、每档仅三次；不宣称因果提速、严格持平或稳定尾延迟。
SSE 可能成批发 token，没有精确 token 时间戳，不推导 ITL 分位数。

## 内存观察与交接

每秒读取 /proc，字段分开报告：

| 运行 | 样本数 | 采样 RSS 最大(GiB) | 观察到 VmHWM 最大(GiB) | MemAvailable 最低(GiB) |
|---|---:|---:|---:|---:|
| MTP 04 固定质量 | 265 | 3.766 | 3.766 | 16.630 |
| 普通 04 五档 | 862 | 0.879 | 1.668 | 29.995 |

两组工作负载不同，不能据此计算 MTP 的物理内存增量。RSS/HWM 不代表
CUDA 全部占用，系统 MemAvailable 是另一种指标；秒级采样可漏瞬时峰值，
整体物理 RAM 上限仍未知。旧基线的内存采样从中途开始，仅作历史观察。

所有本阶段本机证据根为 `.q4t-work/mtp-admission-20261007/`：

- `source/build/`：各代构建/源码身份、直接测试与首次失败日志。
- `source/.q4t-work/evidence/`：quality、boundaries、limits、performance、
  cancellation 与 `mtp-fresh-control-04` 原始请求/响应/日志。
- `candidate-01-archive/`、`candidate-03-archive/`、`repro-old/`：旧二进制、
  库和独立反例；`linear-capture-01/`：唯一有界采集与离线分析。
- `mtp-weight-metadata-review.json`、`mtp-weight-allocation-review.json`：
  实际映射与加载分配核对；`stage-summary.json`：离线验收汇总。
- `summarize-stage.py` 与 `run-mtp-fresh-control.py`：本机复核脚本；
  后者的确切副本和输入摘要也保存在控制证据目录。
- `protected-workspaces-check.json`、`final-runtime-check.json`：原两处
  dirty diff/两个二进制保持、124 个运行文件身份、测试服务停止确认。

旧主线五档位于 `.q4t-work/main-wrapup-20261007/source/build/` 下的
`mtp-admission-baseline-off-20261007/`。main 保持 `8cd297b`，原目录
7 项修改和 wt-c1 的 2 项修改未纳入，模型和 reference/ 未改动。
后续可独立审查本阶段修复；MTP 启用需要另行闭合实际路径数值合同。
