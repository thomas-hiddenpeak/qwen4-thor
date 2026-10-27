# 完整会话缓存：验收结果与交付边界

阶段起始2026-10-27，验收决定2026-10-28：
**ACCEPT_BOUNDED_LONG_SESSION_CACHE**。四组冻结收益目标达标，正确性与
适用五档测量完整；负向观察和统计限制保留。**安装健康已完成，程序已交付**，
默认仍不启用会话缓存。最终判定和安装终态分别记录在下方。

用户要求主agent等待工具/子代理时释放执行槽，槽位被其他请求使用后仍可
恢复原会话。本次实现同一服务进程内、显式session_id的完整前缀快照。
客户端仍发送完整历史；服务精确匹配token前缀，命中后从SSD恢复状态，
只计算新增内容及最多4个旧尾部token。执行槽释放，启动时预分配的槽位
RAM和模型权重不释放。不是任意生成位置的中途抢占，也不是跨重启续跑。

本轮缓存HTTP收益材料使用预渲染`prompt`。`messages`共用编码后的恢复路径，
但本轮未集成特定agent SDK。客户端必须保留原始序列化历史；结构化消息
需正确分离reasoning_content/content/tool_calls并维持模板配置。当前模板
会trim消息、重新包裹thinking和序列化工具调用，重新渲染未必与原生成文本
逐token一致；不一致时安全miss，不能把raw-prompt收益自动授予任意SDK。

## 身份和状态合同

- 父版main cb4b085，程序f2a6de360e05676c5fee2f86fbd314c9a9ff3ea0378fa104e0185cef6b4ec164。
- 实现提交524ce37，候选09fe25ba5db65ae0bd343a110f2b7042de06f1bc7184bba51d10704638ad26b3。
  build-4之后运行时源码与程序冻结，后续修改只有夹具、分析与文档。
- 65536行BF16 head复用既有产物，manifest SHA
  b63b0f251fddc8a75749a54dd4b22477fdb8da875cc26f83595534c71062c3ce。
  完整head仍默认，suffix和会话缓存分别显式启用。
- 适用配置：文本、greedy、MTP k=3，显式槽位208896+3×65536，prefill4096；
  首版不组合媒体、router trace或SSD专家卸载。不同head不共享会话缓存。

保存范围包含所有目标层的SSM、短卷积、PLE卷积、有效KV、索引raw与完整
压缩组、三行RoPE，以及草稿KV/索引、rolling g、d0、pending token、最后
一行目标trunk和全局token历史。恢复使用目的槽位自己的固定页表；先reset
目标/草稿并使临时checkpoint失效，suffix索引按已提交历史重建。
工具结果可能替换先前预测的pending token，此时用已保存trunk修复草稿最后
一行；随后新增内容prefill更新g/d0。EOS缓存请求补齐最终draft extend。

同session并发返回409；未启用缓存却传session_id返回400。历史前缀不一致、
过期、淘汰或非GPU读写/完整性错误均安全miss并正常prefill；部分写入恢复
失败会重置两个模型。GPU错误关闭当前推理健康状态。取消/失败不发布新快照，
旧有效前缀即使保留也必须重新精确匹配。只在正常HTTP结束发布快照。

## 正确性

必要构建后的首测试为tools/evalscope真实HTTP，两head合计22条精确质量通过，
包括200K。新增存储合同49项，使用实际O_DIRECT文件，覆盖分块/对齐、完整
往返、并发租约、前缀不符、改变pending、取消、损坏/截断/复制失败、TTL和
8项LRU。HTTP覆盖保存→其他请求覆盖槽位→恢复、前缀miss、取消后重新请求。

两head各一次直接GPU合同使用真实4目标层（含3linear、1full及PLE）和真实
draft，独立枚举持久状态。源槽0被其他请求覆盖后恢复到槽2，与保留的参考
槽1逐字节一致；改变pending的一行修复与独立草稿重放一致；继续17token的
状态/logits相同；终止draft更新与独立extend相同。真实HTTP仍使用全部48层。
这证明受测接受前缀状态合同，不宣称任意输入与fresh prefill位级输出等价。
旧接受0/1/2/3、suffix/神经切换、子集全局ID与32短请求排队的未变证据复用。

D的45K跨轮记忆检查两head均通过：首请求回答7并保存，第二请求恢复
45042个token后正确回答原历史审计码711146。五档45对请求完整输出均一致。
五档保护请求不带session_id，full/subset候选服务全局启用缓存，父版关闭；
plain两臂不启用缓存。该矩阵保护现有未选择缓存的请求，不代表短请求主动
传ID的额外状态复制/终止extend成本已获性能验收。短前缀不足8192不保存，
建议只给确实复用的长会话传ID。200K是普通请求保护，缓存恢复的长输入
收益范围为约128K主会话，不能写成已验证200K缓存命中收益。

## 两head端到端净收益

每场景4并发，首轮约128K主agent和32K/48K/60K子代理，输出各2048；
中间4条其他请求覆盖全部物理槽，各输出256；原会话带新增内容恢复，
各输出256。两轮事务包含首次保存、干扰和恢复，合计10240个输出token。
不包含服务启动和外部工具自身运行时间；不能解释为任意agent任务整体提速。

| head / 材料 | OFF事务秒 | ON事务秒 | 耗时减少 | 吞吐增加 | 恢复TTFT减少 |
|---|---:|---:|---:|---:|---:|
| full / 重复代码 | 873.788 | 631.660 | 27.710% | 38.332% | 94.84%～94.95% |
| full / 重复文本 | 871.519 | 629.231 | 27.801% | 38.505% | 95.34%～95.46% |
| subset / 重复代码 | 871.533 | 630.088 | 27.703% | 38.319% | 94.88%～94.99% |
| subset / 重复文本 | 878.317 | 636.421 | 27.541% | 38.009% | 95.34%～95.45% |

四组均满足事前事务耗时减少≥10%、每路恢复TTFT减少≥50%的目标，所有重复
输出及恢复前缀检查通过。完整head不能替代子集结论。首轮保存代价明确保留：
full代码417.762→426.902秒，full文本417.683→426.461秒，subset代码
417.083→426.488秒，subset文本423.800→432.615秒，约增加8.8～9.4秒。
只有一次请求或不复用前缀时，没有这些恢复收益。

每head/材料只有一个描述性配对，没有跨启动方差估计或总体非劣置信区间。
full代码OFF首轮/干扰复用原服务有效8请求，第二轮来自新服务，明确保留
这一启动层级差别。C的缓存开关对照均为同一候选程序，suffix保持开启。
每臂新服务，SSD会话缓存从空开始；不清OS页缓存，不增加warmup请求。
C最终子集顺序ON→OFF，D顺序full父→候选、subset候选→父、plain父→候选；
不同head的顺序不能充当同一head的多个独立启动配对。

## 内存、SSD和等待成本

固定8项、24GiB SSD总预算（含写入中快照）、30分钟TTL和空闲LRU；
TTL在访问时检查，非后台定时删除。私有0700的非tmpfs目录，匿名0600
O_TMPFILE|O_DIRECT文件；不支持直接IO就启动失败，不回退文件页缓存。
退出或淘汰关闭fd回收文件，同一进程内恢复，不支持重启持久化。

单8MiB共享host staging，命名payload上界18214564字节（约17.4MiB），
启动预算额外预留32MiB。此payload估计不含allocator内部碎片/记账和CUDA
pageable复制内部暂存，不能用它宣称实际增量RAM精确等于17.4MiB。
目标/草稿KV和递归状态仍是既有固定GPU槽分配，SSD快照没有另一整份RAM镜像。

C受测SSD峰值18.451GB（十进制，小于24GiB上限）。full/subset ON最小
MemAvailable分别11.766/12.528GB，q4t VmSwap均0；进程RSS约4.13GB，
与关闭臂的跨启动差约5～9MB，仅作观察，不能当作隔离的功能增量RAM。
整机已有约2.147GB swap占用，不能将q4t VmSwap=0写成整机无swap。
全机物理使用量包含可回收缓存/内核/驱动，不再叠加RSS和CUDA分配。

保存/恢复持有模型锁，CPU复制、SHA256、磁盘IO和其他槽位等待都计入E2E。
save日志计时还含获取模型锁的等待，多请求区间重叠，不能简单相加为纯IO；
restore日志从持锁后开始，TTFT包含此前等待。资源JSON保留索引初始化/
追加/查询/释放成本、接受分布、verify/draft-extend工作量和监控全量口径；
本轮未增加隔离的GPU区间计时。外部服务启动窗口包含直接IO探测，内部
startup_total计时开始较晚，不能将其用作完整cache初始化成本。

缓存ON的四路恢复轮次工作如下，接受分布按接受草稿0/1/2/3个计数；
verify/extend是token行数而非GPU毫秒。完整计数见suffix-work-summary.json。

| head / 恢复材料 | suffix命中率 | 接受0/1/2/3 | verify行 | 神经draft行 | extend行 | 索引初始化/查询毫秒 |
|---|---:|---|---:|---:|---:|---:|
| full / 代码 | 87.302% | 0/0/0/252 | 1008 | 64 | 1008 | 1.963/10.903 |
| full / 文本 | 87.302% | 0/0/0/252 | 1008 | 64 | 1008 | 1.899/11.973 |
| subset / 代码 | 87.302% | 0/0/0/252 | 1008 | 64 | 1008 | 2.183/8.824 |
| subset / 文本 | 83.333% | 16/0/0/248 | 1056 | 88 | 1008 | 1.957/10.677 |

四路索引追加合计0.152～0.214ms，释放合计0.0049～0.0068ms，查询含数字
分类与相关等待，不另外重复加numeric_ns。首轮full两材料命中98.239%，
subset代码/文本96.339%/91.481%；子集首轮有40/152次零接受。命中率和工作
计数用于解释，接受决定仍使用两head各自完整E2E净收益。

## 证据纠错与有界性

原失败记录均保留，没有把STOPPED追改成COMPLETED：A两项工具断言分别
误等不存在的released事件、误要求零命中也分配分类缓存；4+6项CPU回放
修复后直接复用有效HTTP。原C第二轮关闭缓存的3子请求合法自然EOS，
不能当成缓存缺陷或等长配对；只重测受影响4条，第二轮统一固定256输出，
首轮2048和材料不变，修订在任何ON数据采集前完成。原full ON最后的取消
恢复题实际710130正确，夹具误拿另一同长度题答案710003；4项CPU回归
按请求SHA核对冻结oracle，26条原HTTP只读复核，不追加模型请求。

这些修订明确记录了原自设修复次数/停止规则的调整，没有悄悄扩大总资源
预算。停止阶段缺显式最终监控屏障的事实保留；已采集样本全flush，无监控
错误，服务正常退出。后续subset和D阶段仍须完整最终屏障。

原240HTTP、14服务启动、4直接GPU fixture、21600秒与绝对截止
2026-10-28 01:47:16 +08不变。D结束累计231HTTP、13启动、2fixture；
安装后健康实际执行预留2HTTP/1启动。每GPU阶段完整驱动持共享独占锁；不自动
扩样、换材料或扫描参数。原始证据与失败保存在.q4t-work/session-cache-20261027。

## 五档保护与判断

D完成94请求/6服务启动，6244.736秒；完整head、子集head、普通decode各自
五档父候选对照，每档3请求。每格为候选相对父版的百分比变化：E2E/decode
正值更快，TTFT负值更快。全部45对输出SHA一致，没有删除负向观察。

| 模式 | 名义上下文 | E2E吞吐变化 | decode变化 | TTFT变化 |
|---|---:|---:|---:|---:|
| full | 1024 | +0.0347% | -0.1403% | -1.3281% |
| full | 4096 | +0.4768% | +0.5317% | -0.3458% |
| full | 8192 | +0.1374% | +0.1155% | -0.1645% |
| full | 45056 | +0.0415% | +0.3935% | +0.0252% |
| full | 204800 | -0.0272% | +0.0851% | +0.0305% |
| subset | 1024 | +0.4245% | +0.2730% | -1.4781% |
| subset | 4096 | +0.1393% | +0.1284% | -0.1627% |
| subset | 8192 | -0.1030% | -0.2178% | -0.0271% |
| subset | 45056 | -0.0084% | -0.2712% | -0.0372% |
| subset | 204800 | -0.0244% | -0.1127% | +0.0218% |
| plain | 1024 | +0.1273% | +0.1108% | -0.4155% |
| plain | 4096 | -0.1378% | -0.1776% | -0.0823% |
| plain | 8192 | +0.0369% | -0.0237% | -0.1969% |
| plain | 45056 | +0.0800% | -0.2722% | -0.2397% |
| plain | 204800 | +0.0558% | +0.2986% | -0.0338% |

最大负向观察分别为plain4K E2E−0.1378%、plain45K decode−0.2722%、full200K
TTFT+0.0305%。这些差值不是已证明的总体非劣，也没有被其他档收益抵消。
两head十组的接受分布、verify行、神经draft行、extend行、suffix查询和命中
计数全部相同；不传session_id时没有新快照复制或IO，代码仍存在少量host
分支和全局开关下的标志vector成本。工作量相同不等于耗时相同，未隔离
这些host成本，不能证明所有负点都是噪声，也不能排除真实微小损失。
本轮未确认可归因退化或关键合同失败，按冻结的有界工程出口接受指定身份
与长会话用途；没有设事后统一百分比容差，没有追加采样。

D的6245次监控全flush，0未flush、0sampler失败、0cleanup错误，最终屏障
通过，外层GPU guard COMPLETED。3条退出窗口pending均由后续身份/退出证据
闭合，active=0；原权限错误的成因未证，不将其写成从未发生权限错误。
监控采样不能排除短于采样周期的干扰。D最小MemAvailable12.402GB，q4t
VmSwap=0；监控频率保持GPU1575MHz、显存4266MHz，温度范围47.5～79.1℃。

## 四项决定

| 字段 | 本轮结论 |
|---|---|
| 证据有效性 | 有效，保留工具断言纠错、原STOPPED、原阶段缺最终屏障、退出观测限制及完整身份来源 |
| 测量完整性 | 完整；22精确质量、两head独立状态/跨槽恢复、四组两轮事务、三模式五档90请求和4跨轮记忆请求均已执行；明确复用未变证据 |
| 统计结论 | 四组描述性收益目标达到；单次启动配对不能建立总体非劣、通用损失界限或任意工作流收益 |
| 工程决定 | ACCEPT_BOUNDED_LONG_SESSION_CACHE；显式启用、受测长会话配置，接受已披露成本与未排除的微小性能风险；不扩大默认或SDK接入范围 |

这仍是按性能优化流程完成的接受，没有改称普通业务推进来豁免评估。
默认完整head、MTP k3保持；suffix和会话缓存默认关闭，子集显式选择。
原两head suffix合同及受益记录只按未变依赖复用，新缓存净收益使用本轮
各head自己的实测。会话缓存减少重复prefill，不替代原suffix机制。

安装健康前实际累计231生成HTTP（228成功、3预期取消）、13服务启动、
2直接GPU fixture；剩余健康2请求/1服务启动。原240/14/4/21600秒及
2026-10-28 01:47:16 +08截止未延长。终审时墙钟约4小时40分，尚有约80分钟。

完整独立归档`/home/rm01/models/dev/qwen4-thor/.q4t-work/session-cache-20261027/evidence-archive-v1`包含2484文件、
1237862388字节，manifest SHA `bccac6abe5d4902815e177cfd658098a9a4045666e243835fd857ef083837da4`；
原完整head C的604原始哈希全部复核不变。原路径和失败记录同时保留。

## 发布与回退

.q4t-work中的发布包和安装健康均已完成。使用
`.q4t-work/session-cache-20261027/delivery-package`保留父版程序/sidecar/真实
CMakeCache、候选程序/源码清单/构建日志、同一子集head及接受证据；逐文件
SHA封存后已替换`build/q4t`与发布元数据。两文件各自原子替换，使用持久
恢复标记和已知身份回退，不声称跨文件断电事务。安装或健康失败恢复f2a6。
健康使用完整阶段共享GPU锁，full安装后2请求验证保存/恢复，subset复用
D相同程序/artifact的跨轮记忆及质量；不额外增加subset加载。

2026-10-28 00:35完成安装健康、00:36完成交付终态核验。程序09fe25ba，
sidecar SHA `5027b9d4229e97edf35fa482e0351581434b8e270cebd8b91b055a671ec0082e`；
164运行时源码与编译/测试输入一致。健康2请求/1启动，恢复9227token且答43，
53监控全部flush、pending=0、最终屏障和GPU guard通过。恢复标记已移除，
没有本阶段常驻服务。累计233生成HTTP（230成功、3预期取消）、14服务启动、
2直接GPU fixture；至交付终态墙钟17345.655秒（约4小时49分），原上限和
绝对截止均未超。没有追加模型请求。

安装前接受决定和包内acceptance.md保留当时“待健康”的快照，部署终态以
[closure](evidence/session-cache-20261027/closure.json)及
[delivered](evidence/session-cache-20261027/delivered.json)为准，不能因旧快照
再次触发等待或重验。健康原始证据另封存delivery-evidence-v1（28文件、
3197789字节）。发布包seal SHA为
`8d27634d5911b63137c161545a2c86f26e4a872aa18d06290f3ac40a32ae401d`。
本阶段实现与收尾记录位于codex/session-cache工作分支，未自动合入main。

关闭功能：移除`--session-cache-dir`并重启，客户端停止传session_id。
整个程序回退须先停止自己的服务，再在共享guard下运行：

```bash
python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/session-cache-rollback/lease -- \
  /home/rm01/models/dev/qwen4-thor/tools/evalscope/.venv/bin/python -B \
  /home/rm01/models/dev/qwen4-thor/tools/session_cache/deliver.py rollback \
  --rollback-output /home/rm01/models/dev/qwen4-thor/.q4t-work/session-cache-rollback
```

回退输出目录必须是新的路径，工具核对封存包、当前身份、同一guard及
回退后的程序/sidecar。不要只替换程序而保留另一个版本的发布元数据。

启动命令及客户端要求见[README使用说明](../README.md#完整会话缓存)。

## 证据入口

- [交付终态](evidence/session-cache-20261027/closure.json)、
  [健康结果](evidence/session-cache-20261027/health-result.json)、
  [安装前接受决定](evidence/session-cache-20261027/final-decision.json)、
  [身份/请求/环境终审](evidence/session-cache-20261027/final-audit.json)、
  [独立归档收据](evidence/session-cache-20261027/archive-receipt.json)。
- [四组收益](evidence/session-cache-20261027/performance-gate.json)、
  [完整保护原值](evidence/session-cache-20261027/protection-comparisons.json)、
  [保护工作量复核](evidence/session-cache-20261027/protection-work-review.json)。
- [full资源](evidence/session-cache-20261027/resources-c-full.json)、
  [subset资源](evidence/session-cache-20261027/resources-c-subset.json)、
  [D资源](evidence/session-cache-20261027/resources-d.json)、
  [suffix工作量](evidence/session-cache-20261027/suffix-work-summary.json)、
  [RAM命名payload](evidence/session-cache-20261027/ram-allocation-accounting.json)。
- [状态合同及冻结方案](SESSION_CACHE_PLAN_2026-10-27.md)、
  [中间进展与原失败](SESSION_CACHE_PROGRESS_2026-10-27.md)。
