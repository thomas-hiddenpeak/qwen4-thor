# NVFP4 草稿 head：原型与首轮采集，验收因监控收尾中止

2026-11-05采集，2026-11-06完成独立复核和驱动CPU修复。
最终阶段状态 **ENV_ABORT_MONITOR_FINALIZATION**。
默认程序、sidecar、main及旧固定包保持。原型已实现、零警告构建，
56个HTTP响应及44项固定质量检查完成，但监控收尾失败，不能据此
接受性能优化，也不将环境中止判作候选NO_GO。

## 实现和实际身份

从 main d8880693 提取到隔离分支 `codex/mtp-quantized-head`；未修改
研究40768107。执行源码 `2dd98b1cd0ca280f948ce5e0e8b65a74623d655a`，
候选 `1dbbc774acd38b307a7418245289664efb32fa7638d7319ce754d498c1fa4f01`，
父/default `c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636`。
sidecar仍为 `0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3`。

新增默认关闭的 `--mtp-head-quantization nvfp4`：从实际加载的 BF16
head 在线派生 E2M1/每16元素E4M3副本，激活BF16、解码权重显式舍入
BF16、点积FP32固定归约。目标BF16验证head保持；首版仅单槽文本
k=3，与subset/suffix/会话缓存/前缀缓存/required启动计划互斥。
量化预填充两个入口只计算消费的末行；验证后extend仍按原API计算
所有行。详细格式、资源及原预算见[判定卡](MTP_QUANTIZED_HEAD_PLAN_2026-11-05.md)。

新增权重357580800 bytes（341.015625 MiB），没有完整BF16草稿副本。
目标head仍驻留1212.5 MiB；已接受的BF16 subset连ID仅320.25 MiB。
这是草稿读取/计算优化研究，不是模型总RAM降低或容量offload。
新权重及4096-byte device/同尺寸host加载暂存已纳入预算。

## 采集原值：只作观察，不作正式性能结论

四臂均single-slot、max_prefill=8192、max_len=208896、startup-plan off，
同snap-records 1K输入三次、各256输出；每臂11个固定召回题覆盖
1K/4K/8K/45K/200K。每臂单次启动，不支持统计非劣或广泛任务质量结论。

| 模式 | 三次decode (token/s) | decode中位数 | TTFT中位数 (s) |
|---|---|---:|---:|
| 已接受plain | 18.530 / 18.599 / 18.572 | 18.572 | 0.770 |
| 已接受完整BF16草稿 | 35.531 / 35.534 / 35.535 | 35.534 | 0.912 |
| NVFP4原型 | 36.402 / 36.350 / 36.415 | 36.402 | 0.902 |
| 已接受BF16 subset | 38.548 / 38.599 / 38.540 | 38.548 | 0.899 |

按点估计计算，候选相对full为+2.445%，相对subset为−5.566%；前者
低于事前5%投入目标。因最终监控中止，这些只是算术观察，既不作
候选NO_GO，也不据此进入五档或部署。首请求TTFT分别0.903/1.078/
1.081/1.069秒，未剔除；完整原值在[独立核对记录](evidence/mtp-quantized-head-20261105/review.json)。

四臂各11质量题均命中期望答案、usage和终止正确。候选14响应与
full MTP逐字及长度一致，subset也一致。plain的1K性能提示输出与
已接受full MTP不同，质量11题相同；不能声称候选与plain任意输入
逐位一致。此差异在两组未修改父ELF间已经存在，本轮没有将其归因
于量化，也不以HTTP质量取代新内核独立数值合同。

正常日志中前三请求full的draft_counts均为8,6,7,53，量化均为
7,9,6,53，平均接受草稿/步由179/74≈2.419变为180/75=2.400。
不能把上游DGX Spark收益直接套到Thor或只比较压缩字节数。

## 两个驱动问题与证据处置

1. 原阶段完成plain的14响应、服务退出0后，错误要求
   server_destroyed时所有配额live归零，停止于309.584秒。父版该
   时点仍有200953856 bytes/8项计费，pending/unknown/配额失败均0。
   该采样点早于进程退出，已有进程级算子存储可继续存活；未对
   全部字节作归属审计，不把它们全部称为缓存或证明无泄漏。
   修正后只读复用14响应，原STOPPED/COMMAND_FAILED记录不改。
2. 剩余3模型/42响应于894.741秒完成，外层guard返回COMPLETED，
   但采样器停止时还保留最后subset服务PID3638917、start_ticks
   208351577的一个退出待确认身份。895次采样全部写出，summary
   明确ENV_ABORT；旧legacy驱动未在join后复核，把状态写成
   MEASURED_REQUIRES_REVIEW。缺少最后一次完整退出后采样，不能
   仅凭外层返回0忽略内层失败。

最初派生的decision和第一次离线review漏查了第二项，均保留原件，
由 `feasibility-final-review.json` 明确作废其NO_GO结论。正式状态
是ENV_ABORT，未增加请求或截止时间来重采。独立复核重读本轮生成
的8个SQLite库，核对56响应、输入/输出摘要、长度、终止、期望答案，
并验证原件seal及原/续跑plan；执行时来源通过对应提交恢复核对。

驱动已改用现成的 `tools/mtp_suffix_zero_hit/run_rerun.py` 包装层：
等待退出后的新完整采样，再检查线程join后summary及待确认身份。
分析入口另要求state/summary/postflight摘要与屏障匹配，失败即拒绝
生成性能决定。7个CPU回归通过，含真实失败模式、late failure、
缺失/过期屏障、pending与摘要变化；另将本轮真实失败plan交给分析
入口，确认返回EnvironmentAbort且不覆盖原decision。修复后没有
新增模型/GPU请求。
复用包装层的实机历史证据不等于本候选已重验通过。

两次HTTP租约310.504/895.492秒，共1205.996秒，284/818次外层观察
未报竞争；轮询不能排除短于间隔的干扰。当前4个服务PID均退出，
8762不监听，NVML无计算PID，原锁dev66305/inode3294067可获取。
无自动恢复服务或默认部署。完整原件在隔离工作区的
`.q4t-work/feasibility{,-resume}/`，原候选62a3d445未运行，仍保留。

## 后续边界与缓存检查

本阶段保留研究入口，不合main、不修改默认、不追加五档、直接GPU
合同或profile。下一阶段若继续，先确定新的有限验收范围；当前尚欠
新kernel独立舍入/反量化/行不变/非法输入与清理合同、有效监控下的
HTTP判定，以及适用五档保护，不能用本轮响应通过替代。

可进一步核对的局部方向是 `MtpSpeculativeStepMulti` 在extend后仅
使用每条序列末行logits，新SIMT却计算所有行。此处减少无消费输出
是源码假设，未profile或干预证明实际收益，本轮不再实施或扩大预算。

缓存静态对照另记录，未运行额外缓存测试：

- [vLLM 59759](https://github.com/vllm-project/vllm/pull/59759) 的
  speculative scratch发布问题：当前SessionStateRanges覆盖完整
  target SSM/conv、QSA raw/compressed/KV和draft/rolling_g；发布在
  完成边界且HTTP成功后。没有因上游问题标题推定本项目存在同一缺陷。
- [vLLM 61589](https://github.com/vllm-project/vllm/pull/61589) 的
  lookahead/shifted边界：快照保存pending，恢复遇用户替换末token时
  单行修补draft；部分恢复失败会重置两模型后再回退。
- [vLLM 61601](https://github.com/vllm-project/vllm/pull/61601) 的
  PLE checkpoint：本项目快照范围已有PLE卷积状态。
  复用[已有两head评分/代表状态与24缓存HTTP证据](MTP_PREFILL_COMPLETION_RESULT_2026-11-05.md)，
  仅说明原接受组合；代表层合同不是48层逐层证明，量化组合已通过
  CLI拒绝缓存启用，尚不属于缓存已验范围。
