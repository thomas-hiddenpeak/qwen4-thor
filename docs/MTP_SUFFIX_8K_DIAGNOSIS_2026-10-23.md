# 固定 k3 suffix 的 8K 回退离线诊断

2026-10-23。按用户后续要求研究已封存的 8K 下降，复用原验收记录，
只做 CPU 重放和源码分析，新增 GPU / HTTP 请求均为 0。未修改运行时、
匹配参数或默认程序；原
[NO_GO_PROTECTION_REGRESSION](MTP_SUFFIX_K3_2026-10-23.md) 保持。

## 结论

本次有直接证据的机制是：8K 输出大量记录编号，三 token 的数字、空格、
逗号短匹配能通过首个 d0 门，却经常把旧编号或字段分隔符接到新位置。
15 次 suffix 命中仅 5 次完全接受；省掉部分神经草稿后，目标四行 verify
反而从 69 轮增加到 73 轮，checkpoint restore 从 10 次增至 19 次。
这解释了为何“有重复且命中”仍会增加目标工作。

代码确有约 8K 的注意力索引分支，但第一次候选分歧发生在绝对位置
8175、分支之前。分支不是候选质量下降的必要条件；它对后续计算成本
的放大或抵消尚无逐阶段计时可量化。不能把全部毫秒损失归给单一因素。

| head | suffix OFF→ON decode tok/s 中位 | 变化 | TTFT 后耗时中位增加 |
|---|---:|---:|---:|
| 完整 | 37.738→36.037 | -4.51% | 318.86ms |
| 65536 BF16 子集 | 42.514→40.006 | -5.90% | 376.05ms |

每臂仍只是原来的同次服务启动内三请求；不增加样本，不声明跨启动显著性。
实际输入为 8159 token，四臂各三请求均输出相同的 256 token，长度终止。
下降不是 ON 生成了更长答案。本次输出是截断的思考片段，质量结论仍
限原质量集样例，未评估这条请求的完整技术评论质量。

## 短匹配具体错在哪里

以下来自生产 tokenizer 与 SuffixDrafter 的 CPU 重放。位置是输出 pending
token 的零基索引；匹配长度以 token 计。实际续写列包括被目标模型更正
后的后续内容，不是三个都被接受的草稿。

| 位置 | 匹配长度 | 精确匹配文本 | suffix 三 token | 实际后三 token | 接受数 |
|---:|---:|---|---|---|---:|
| 16 | 3 | ` 25` | `1: A` | `1 records,` | 1 |
| 157 | 3 | ` 10` | `9: A` | `9, ` | 1 |
| 171 | 3 | `, 1` | `23,` | `24,` | 1 |
| 215 | 3 | `5, ` | `163` | `166` | 2 |
| 70 | 11 | ` cache can reuse recently accessed data. An estimate of required` | ` bytes is different` | ` bytes is different` | 3 |
| 78 | 19 | ` cache can reuse recently accessed data. An estimate of required bytes is different from a measurement of physical` | ` memory traffic.` | ` memory traffic."` | 2 |

第一例正在生成 “251 records”，却复用 prompt 中 “Record 251: A ...”
的尾部。首个 `1` 正确，因此 d0 门允许使用该候选；冒号和 `A` 并不适合
当前位置。OFF 在输出位置 16 一轮发出 `51 records`，ON 先发 `51`，
下一轮才发 ` records`。这个额外工作在注意力阈值前就出现。

位置 171 的四个候选都只匹配 `, 1`，续写分别为 `23,`、`16,`、`09,`、
`7, `。最长匹配平局时选择最新候选，拿到了前面刚列过的 123，而当前
需要 124。逐 token 比较和确定性选择都正确执行，问题在于匹配前缀
对接下来两 token 的预测力不足。

15 次命中中，数字/字段格式占 8 次，均为 3-token 匹配：7 次接受 1 个，
1 次接受 2 个，没有全接受。指令复述 2 次接受 3/2 个；模板引用 5 次中
4 次全接受、1 次在结尾标点处分歧。十次最短匹配也有两次全接受，
19-token 长匹配仍会部分拒绝，所以不能据此宣称某个新最小长度已解决
问题。全部 15 次、历史来源及候选 ID 见
[索引取证](evidence/mtp-suffix-8k-study-20261023/index-study.md)。

## 多出来的工作，以及没有测到的成本

两 head 的原始接受分布及重建步序完全相同。下表覆盖每请求整个生成，
含首个 MTP 轮、排除 prefill 与 MTP 初始化；计数由原日志与实际控制流
推导。latency−TTFT 则排除了首 token 之前的工作，两者不是相同时间
窗口，表中计数不能直接当成该耗时指标的精确分解。

| 工作 | OFF | ON | 变化 |
|---|---:|---:|---:|
| 四行 target verify | 69 | 73 | +4 |
| 接受 a0/a1/a2/a3 的步数 | 5/2/3/59 | 6/9/4/54 | 部分接受增多 |
| target checkpoint restore | 10 | 19 | +9 |
| 普通尾部输出 token | 2 | 4 | +2 |
| 普通尾部 target T1 forward | 1 | 3 | +2 |
| target 总处理行数 | 277 | 295 | +18 |
| 神经 proposal T1 forward | 138 | 116 | -22 |
| draft extend 调用 | 69 | 73 | +4 |
| 全部 MTP forward 调用 | 207 | 189 | -18 |

首个普通尾部 pending 无需先 forward，因此不能把尾部输出数直接当作
forward 次数。原日志 scheduler_waits 为 70→76，也与 target 调用闭合。
不同 T 的调用成本不同，不能拿总调用次数直接换算吞吐。

固定 k3 每个神经 proposal 轮调用两次 MtpForward；15 次 suffix 命中
本可少 30 次，但多出的四轮抵回 8 次，净少 22 次。目标 verify、状态
恢复和 extend 仍保留。按当前 36 个 linear 层与 1 个 PLE 层的状态
维度，额外 9 次 restore 请求约 1.041GB D2D 复制 payload、657 次
cudaMemcpyAsync。这是静态请求字节数，不是实测物理 DRAM 流量、驻留
RAM 增量或可换算的 GPU 耗时。

原日志中每请求索引 append + query + release 平均约完整 head 59.5μs、
子集 86.3μs，远小于约 319/376ms 的 E2E 增量。初始化约 113.2/105.7μs
归入 TTFT；未发生索引增长分配。记录到的索引 host 时间不足以解释该
回退，但这不构成对所有间接缓存影响的测量。

同一文本位置分段的客户端 SSE 时间还显示，Records 编号列表及尾部
增加约 320/335ms；此前内容跨度分别变化约 -1.6/+42.5ms。它支持额外
等待集中在编号部分，但客户端解析到达时间不是 GPU phase 时间，
不能逐毫秒归因到 verify、restore 或注意力分支。

子集的草稿投影只有完整 head 的 26.4% 行数，两边却增加同样的目标
verify 与恢复工作。因此子集省下神经草稿的价值较低，是其净下降更大
的合理解释；原 8K 没开逐阶段计时，尚不能给出各项实测毫秒占比。
其他场景的诊断计时不挪作 8K 分摊。详见
[工作量原值](evidence/mtp-suffix-8k-study-20261023/runtime-work-counts.json)。

## 8K 阈值与内容效应的边界

`src/model/full_attention.cu` 中索引组数为
`floor((max_position + 1) / 4)`，超过 2048 进入长分支。因此首个长分支
max_position 为 8195；四行 verify 的末位置是 P+3，故 P≥8192 时进入。
8159-token prompt 之后约提交 33 token 就会跨过它，OFF/ON 都会跨过。
第一次步序分歧在 P=8159+16=8175；多出的四行 verify 并非只在分支
切换后才出现。未实测该分支的有符号耗时差，不能排除其放大后续成本。

本请求没有触发 max_len=208896 的上下文容量尾部，也没有因 max_prefill
8192 而在 decode 总长度 8192 处分割；后者限制本次 forward 行数。
日志 `groups=10/per-row` 的计划创建也不是该注意力分支切换事件。

五档输入都是同一套 8 种 record 模板、同一随机记录序列的不同前缀，
但生成内容不同：8K 枚举 cache 类别的 38 个编号（与原 prompt 对应），
4K 也枚举编号；45K/200K 主要连续引用模板。45K 命中 30 次、29 次全接受，
verify 轮数不增；8K 命中 15 次、仅 5 次全接受。长档的收益不能证明
“8K 长度本身有 bug”，也不能把跨档长度/内容共同变化当作位置消融。

## 取证方法、身份与收尾

范围限定为两个解释：短匹配候选质量，以及约 8K 的执行分支。原 GPU
独占阶段的结果不重跑；仅编译/运行 CPU helper、读取 SQLite 与源码。
没有参数扫描、模型加载、HTTP、GPU profile 或部署健康请求。

1. 原 SSE 每请求为 role 1条、非空 content 256条、finish 1条，源码按
   单 token Decode/发送。用生产 Decode(skip_special=true) 穷举模型
   ID 域 [0,248320)，每个观测 chunk 唯一反查到一个 token；tokenizer
   有效词表 248077，剩余 243 个 padding ID 全部 Decode 失败。
   不通过整段文本重编码猜测输出 ID。
2. 原客户端间隔自然分出批次：全部请求内短间隔最大 0.322ms、长间隔
   最小 54.458ms。末尾长度 5 的批次按独立日志提交数拆成 MTP+首个
   普通 pending，得到 69/73 轮及全部接受直方图。它是客户端推断，
   不是当时已有的原生 server step trace。
3. 在推断的步起点使用真实 SuffixDrafter 重放。a≥1 时第二个输出
   token 给出 d0；每请求六个 a0 步均无匹配，不需要猜其 d0。两 head
   共六条 ON 请求都复现 queries=73、hits=15、no_match=24、
   d0_mismatch=34、candidates=117、compared=642、matched=201，
   以及 suffix 接受分布 [0,7,3,5]。该重建受到多个独立聚合量约束，
   仍不能恢复被跳过的神经 d1/d2 或 GPU phase 时间。

原受测源码 `cb912543f9a11191135bb77e44d8e1d1d598ed78`，候选 SHA256
`1266ee3404e233514669c9ef4de2c6d5df0ea832a34b70cbcfde7db4ebadf392`；
相关源码与冻结快照一致。原分析 SHA256
`dcbc4ad8f6ff50e3d549c3a05d1a961e405597c2251dfbe078ba2a9de148a86d`
保持。新增原始解析、CPU helper、全量重放及摘要保存在
`.q4t-work/suffix-k3-8k-study-20261023/`，
[研究回执](evidence/mtp-suffix-8k-study-20261023/receipt.json) 绑定文件摘要；
必要原值入 Git，原验收/归档未改写。

工程决定仍为不交付 suffix。后续若另行实现，应研究能区分低信息量短
数字匹配与连续文本引用的候选准入；单看命中率、提高最小长度或按
“8K 档”关闭都不是本次已验证的修复。这里展示的验收输入已经成为
开发诊断材料，不能再作为下一候选独立未见验收材料。新方案需另冻结
开发/验收集合与有界预算，分别验证两 head 的净收益；本轮不启动。

默认 `build/q4t` 保持 f356b2c9，发布 sidecar 保持 422993b0，完整 head
仍默认、65536 BF16 显式选择。诊断文档及证据随现有 `codex/suffix-k3`
分支提交推送，不合入 main，不重开原候选接受决定。
