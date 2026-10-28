# 多槽单请求草稿 prefill 评分：有界验证结果

2026-10-29，**RESEARCH_GO_REQUIRES_RELEASE_VALIDATION**。本轮候选通过：
两种 head 的 200K TTFT 均下降约 34%，独立数值/状态合同通过，四槽容量
保持；主要原因已定位并由同程序消融验证。默认程序仍是 6f3722b2，未部署、
未合 main，不将本轮三档单对定位结果当成正式五档发布验收。

## 冻结范围、身份与预算

[事前方案](MTP_PREFILL_SCORING_PLAN_2026-10-29.md)保持冻结时原文，
其中“尚未测试”是当时状态，本结果为完成记录。用户要求失败即停、不扩样。

| 项目 | 身份/实际执行 |
|---|---|
| 实现提交/分支 | `c9d4475` / `codex/mtp-prefill-scoring`，起点 `dab8321` |
| 父程序/仍安装程序 | `6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965` |
| 原发布元数据 | `5fdca879f84da6795b66113833de7167df6edbcff0d97e09d474113e47a67de2` |
| 候选程序 | `c0ed0800bb36f8c8b7dcf9155ae463dd146d73b217294839f0776bef138f6d2d` |
| 合同程序 | `684bb19de7ecd55411ff2058cf5cef010d8a31ccaa8f86731ca82814ae4fdc5c` |
| 子集 BF16 head | `21522eabb4ab4f816d9f76eefe317a17e7ba668ebe838809eb4be87d84ef75c1` |
| 子集全局 ID | `71d89c6d2899c22641ac3d1f7f4957de94eddc5256b3f60bc8242f24becf590b` |
| 冻结计划 SHA256 | `64ab3b3d93abeb0d4c4644d25123ab9945e47d834d1ad955ff7e915e5a46d407` |
| 执行 | 16 HTTP / 8 服务 / 2 GPU fixture；无重试、无 warmup、无额外生成请求 |
| 阶段墙钟 | 2460.348 秒，即 41 分 0.348 秒，小于 5400 秒及绝对截止 |
| 直接合同 | 两次合计 33.068 秒，小于 1200 秒 |

必要构建零警告。第一项测试是真实 tools/evalscope HTTP。先完成四个服务
共 12 条常规 HTTP，再检查两 head 200K TTFT 各下降至少 20% 且 E2E 耗时
下降，随后两次合同、最后四条同程序评分消融。阶段完整持共享 GPU 独占锁。

配置为 `max-seq=4`、`max-prefill=4096`、
`seq-capacities=208896,65536,65536,65536`，MTP k3、suffix、私有缓存、
公共 prefix 缓存均启用；请求串行，不携带 session/scope，无缓存读写。
每服务新建空缓存目录，不清 OS 缓存。greedy、seed=20260920、256 输出，
实际输入为 8170 / 45044 / 204781 token。完整 head 默认，子集显式选择。
8K/45K 实际使用 slot 1，200K 使用 slot 0，覆盖不等长槽的非零偏移。

## HTTP 收益与全部负向观察

每侧各一条，以下均为秒；“下降”为正表示候选更快。没有重复启动方差估计，
不建立统计非劣，也不从样本波动反推允许损失界限。

| head / 档位 | 父 TTFT | 候选 TTFT | TTFT 下降 | 父 E2E | 候选 E2E | E2E 下降 |
|---|---:|---:|---:|---:|---:|---:|
| full 8K | 6.192 | 6.394 | **−3.266%** | 13.605 | 13.916 | **−2.286%** |
| full 45K | 37.471 | 33.243 | 11.281% | 44.566 | 40.360 | 9.437% |
| full 200K | 262.451 | 173.453 | **33.910%** | 270.197 | 181.206 | **32.936%** |
| subset 8K | 6.089 | 6.040 | 0.818% | 12.699 | 12.659 | 0.316% |
| subset 45K | 37.434 | 33.184 | 11.352% | 43.905 | 39.680 | 9.622% |
| subset 200K | 262.565 | 173.805 | **33.805%** | 270.068 | 181.306 | **32.867%** |

完整 head 200K 少等 88.998 秒，子集少等 88.759 秒；端到端输出吞吐分别
从 0.94746 → 1.41276 token/s、0.94791 → 1.41198 token/s。
这不是首 token 之后的 decode 吞吐；后者统一按 `255/(latency-TTFT)`：

| head / 档位 | 父 decode token/s | 候选 decode token/s | 变化 |
|---|---:|---:|---:|
| full 8K | 34.3987 | 33.9013 | **−1.446%** |
| full 45K | 35.9378 | 35.8292 | **−0.302%** |
| full 200K | 32.9214 | 32.8937 | **−0.084%** |
| subset 8K | 38.5806 | 38.5238 | **−0.147%** |
| subset 45K | 39.4043 | 39.2529 | **−0.384%** |
| subset 200K | 33.9836 | 33.9975 | +0.041% |

full 8K TTFT 增加 0.202 秒、E2E 增加 0.311 秒是本轮真实负向观察。
两侧接受直方图一致，subset 8K 方向不同；本设计不能确认这些小差值的因果，
更不能声称已经消除 8K 波动。没有用 200K 收益抵消已确认退化，也未追加采样。

全部 16 条 HTTP 都通过冻结 prompt、实际输入、256 输出、length 终态及
完整输出 SHA 核对。六个常规父候选配对的 MTP 接受直方图各自一致：
full 三档依次为 `6,11,5,53`、`6,3,5,57`、`4,5,3,58`；
subset 依次为 `7,8,7,52`、`7,5,6,55`、`8,7,5,54`（接受 0/1/2/3）。
本轮 suffix 命中为 0，不以 suffix 或缓存收益解释评分改进。

## 主因与同程序消融

原 `MtpDraftExtend` 即使一次只处理一个已知槽的 prompt，在 max-seq>1 时
仍传非空 `d_seq_id`；`FullAttentionForward` 因而进入逐元素 FP32 SIMT
评分。长上下文和大 T 的标量评分工作很大，原单槽则使用 BF16 GEMM。

候选仅向评分传入 host 已知槽的 `idx_comp` 切片；KV/raw/comp 写入、页表、
RoPE、稀疏 attention 仍按原 pooled 地址及 d_seq_id。混合槽 generation、
T<=4 的 pending 修补和接受后 extend 不进入新分派。未改 head 行数、
位置回读、分配或同步。实现见 [MTP 调用](../src/mtp/mtp.cu)和
[attention 评分](../src/model/full_attention.cu)。

四条诊断均使用同一 c0ed 程序，旧评分仅设
`Q4T_MTP_PREFILL_LEGACY_SCORING=1`；诊断设
`Q4T_PREFILL_SCORING_TIMING=1`。每条 50 块×4 区间=200 事件记录，
总 800 条全部有效。事件在既有完成边界后读取，无新增 stream 同步。

下表是 CUDA **stream elapsed** 总和，单位秒，包含提交空隙，不称纯 kernel
活动时间。父子区间互相包含，不可相加；诊断不充作额外性能接受样本。

| head / 区间 | 旧评分 | 新评分 | 差值（旧−新） |
|---|---:|---:|---:|
| full 目标 prefill | 165.831 | 165.719 | 0.112 |
| full 草稿 prefill | 96.854 | 8.107 | **88.747** |
| full 草稿 attention | 94.271 | 5.544 | 88.727 |
| full 索引评分＋Top-K | 90.927 | 2.204 | **88.723** |
| subset 目标 prefill | 165.456 | 165.738 | −0.282 |
| subset 草稿 prefill | 96.727 | 8.031 | **88.696** |
| subset 草稿 attention | 94.257 | 5.546 | 88.710 |
| subset 索引评分＋Top-K | 90.927 | 2.198 | **88.729** |

评分区间 thread CPU 合计 full 4.087 → 8.250 毫秒，subset 4.119 →
8.377 毫秒；GEMM 提交略多，但远不足以解释旧路径约 91 秒。草稿整体
host wall full 96.854 → 8.106 秒、thread CPU 1.001 → 0.971 秒；
subset host wall 96.726 → 8.030 秒、thread CPU 0.933 → 0.938 秒。
host wall 包括内部阻塞，wall−CPU 不能直接称纯 GPU 等待；stream 区间、
CPU 与 HTTP 三者共同支持主因在评分设备执行链，而非约 89 秒 CPU 提交。
本轮没有 Nsight kernel 活动积分，未冒称精确的所有等待分类。

## 数值、状态与缓存合同

这里存在 FP32 SIMT 与 BF16 GEMM 中间点积的舍入差异，不能走逐位等价豁免。
两 head 各一次真实 draft 合同，使用四个代表目标层；独立参考显式枚举状态，
不借生产范围构造器来证明状态覆盖。生产范围构造器只用于快照 IO。

四个 scoring case：slot3/base0/T17、slot2/base8190/T17、
slot1/base8192/T17、slot0/base12288/T4096。每个都从同一完整池状态开始：
独立 sliced/NULL seq GEMM、新 pooled GEMM、旧 pooled SIMT。
新路径与独立参考的 attention 输出 exact；三者的全部池 KV/raw/comp exact；
其他槽、未写区间、页表和 RoPE 元数据 exact，输出有限值。旧 SIMT 与参考
输出的不同字节数依次为 0、12659、8351、1891911，两 head 相同，如实保留。

缓存合同覆盖 1024/8193 两边界，源槽和目的槽被其他请求覆盖后恢复；
私有查找、跨会话共享查找、四个分支、改变 pending、17 个新增 token，
最终 target logits、g/d0/bonus 及完整枚举状态与无快照的独立延续参考一致。
改变 pending 时采用“原已提交前缀＋单行 pending 修补”的参考合同：
1024 边界与重新整块 prefill 的状态有 5213 字节形状差异，8193 为 0，
不能把整块重算与恢复延续称为任意输入逐位等价。这项观察保留原合同口径。

直接合同是四代表目标层＋真实 draft，不宣称 48 层逐层状态证明；
实际完整 48 层由 HTTP 固定输出覆盖。本轮没有新的广泛语义质量矩阵。

## 容量、资源与监控

所有八个服务均保留 208896+3×65536 四槽和 4096 prefill 暂存，缓存启用。
父候选 startup 预留计划一致：full 1619 分配 / 101.714 GB，subset 1621 /
102.050 GB；结束全部释放。这是预留账本，不能替代整机 RAM 峰值。

GB 为十进制字节；下表覆盖每个服务启动至退出，而不只请求尾部：

| 常规服务 | 最小 MemAvailable GB | 最大进程 RSS GB | 最大进程 VmSwap |
|---|---:|---:|---:|
| full 父 | 10.930 | 4.133 | 0 |
| full 候选 | 11.050 | 4.129 | 0 |
| subset 父 | 10.343 | 4.130 | 0 |
| subset 候选 | 10.681 | 4.126 | 0 |

完整阶段最小可用 9.727 GB（子集旧评分诊断），
`MemTotal−MemAvailable` 最大 122.194 GB；此值包含系统及不可立即回收占用，
不等于本服务精确独占 RAM。host/device/managed 共用 Thor 物理 RAM，
RSS、CUDA 预留和文件缓存不能相加算容量；本轮没有 CPU 容量 offload。

2461/2461 采样全部 flush，130433 次已发现资源源读取均 OK；待退出进程
1 个入队、1 个确认、active=0。最终服务退出后采样屏障通过，postflight 无
监控/清理错误，外层 guard COMPLETED/returncode0，无冲突或不可读进程。
CPU/IO/memory PSI 在发现时不可用，明确记录 UNAVAILABLE，未补零。
1 秒轮询不能排除短于采样间隔的未合作干扰。

## 决定、复用和停止边界

| 字段 | 本轮结论 |
|---|---|
| 证据有效性 | 身份、预算、退出、监控有效；保留轮询/PSI 边界 |
| 测量完整性 | 冻结 16/8/2 全部完成；不是正式五档/缓存命中/四路并发矩阵 |
| 统计结论 | 描述性单对；没有统计非劣和任意输入逐位等价主张 |
| 工程决定 | **有界候选 GO，主因已修复；正式发布验收待另行冻结，不自动展开** |

复用未变的 SSD 快照格式、租约/取消/队列与 suffix 生命周期既有证据，见
[完整会话缓存](SESSION_CACHE_RESULT_2026-10-27.md)、
[公共种子](PREFIX_SEED_RESULT_2026-10-28.md)、
[四槽 suffix 交付](MTP_SUFFIX_AGENT_DELIVERED_2026-10-27.md)。
本次直接重验了受影响的 prefill/恢复状态；旧交付的两 head 重复场景收益
仍对旧身份有效，不自动归给新候选。普通 decode 分派没有改变，但本轮
未重测普通 decode，也不把它写成候选五档性能保持已通过。

下一阶段如推进发布，仅冻结受影响的适用五档、两 head 重复收益和缓存
HTTP；保留本次主因/直接合同/固定输出证据。此次没有自动扩样、再调参、
安装程序、写新默认发布元数据或运行部署健康请求。

## 证据与回退

版本化 [终审数据](evidence/mtp-prefill-scoring-20261029/review.json)、
[冻结计划](evidence/mtp-prefill-scoring-20261029/frozen-plan.json)、
[full 合同](evidence/mtp-prefill-scoring-20261029/full-state.log)、
[subset 合同](evidence/mtp-prefill-scoring-20261029/subset-state.log)及八个服务
原始小日志可直接核查。纯离线重算脚本为
[review.py](../tools/mtp_prefill_scoring/review.py)，不发 HTTP/GPU 请求。
源码/输入/产物 763 项、运行 seal 327 项终审一致，默认程序及 sidecar 一致。

原始目录 `.q4t-work/mtp-prefill-scoring-20261029/` 保留；独立归档
`.q4t-work/mtp-prefill-scoring-20261029-archive/` 包含候选、父程序、子集产物、
源码、输入、全部原始 HTTP/监控日志及构建记录。1106 文件 / 478837466 字节
逐项复制哈希一致，seal SHA256 为
`de7cd9a3e21b0cdfb9cc6a8f82ca23d4e399a5dfbabcfafd138e9d2ae0b2c52a`，
见[归档收据](evidence/mtp-prefill-scoring-20261029/archive-receipt.json)。
无需执行回退：安装身份从未改变；候选与原父程序均有固定保留路径。
