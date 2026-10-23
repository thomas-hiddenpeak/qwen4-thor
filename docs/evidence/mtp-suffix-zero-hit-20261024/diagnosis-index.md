# 零命中开销：索引侧离线诊断（2026-10-24）

本记录只读取当前源码及已封存的 min32 阶段证据，没有构建、测试、模型加载、HTTP、GPU 或生产代码修改。此前验收材料在本阶段均为开发诊断材料，不能再称本阶段 heldout。已读 AGENTS、STATUS、EVALUATION 与 PERFORMANCE_DECISION_PROTOCOL。

结论：现有直接计时不支持用索引微优化解释子集 45K/200K 的主要 TTFT 差，也不支持用数字门分类开销解释新 8K decode 差。唯一保留的小改候选是数字准入遇到首个非 allowed 属性时停止读后续属性，保持候选和准入结果不变；但其可减少的直接工作太少，现阶段不建议为了“有改动”就实施或宣称解决保护项。建议先冻结一次有界归因诊断，再决定是否值得实施这个唯一候选。

## 实际执行与计量边界

- `src/mtp/suffix_drafter.cpp:108` 的 Init 先验证全局 ID，分配并初始化 65536×4 个 uint32 end（1 MiB），分配实际 capacity 的 int32 历史，然后逐 token 复制、三元组哈希和最近四候选插入。没有按生成长度增长的 allocation。当前 capacity=208896，数组 payload=1884160 B，object=248 B。
- `AppendUnchecked:148` 每个提交 token 复制一次；第三个 token 开始执行同一个三元组哈希、移动三个 end、插入最新 end。`Append:160` 先完成容量和全体合法性检查，失败不部分追加。只追加实际提交的前缀；pending、拒绝尾部及普通 decode 尾部不写入该索引。
- `ProposeAfter:182` 查询 `[committed,pending]` 临时视图；同一哈希桶至多四个 end，逐 token 后向精确比较至 64，最长优先、平局保留最新。之后依次检查完整三候选、d0、数字门、min32。min32 不免除前面的查询或数字门工作；不搜索次优候选。
- Init 位于 `chat_generation.cpp:706`，在目标 prefill、完整 prompt 的 MTP 初始化、`cudaFree(d_trunk_full)` 之后，第一次 MTP step/SSE 之前。请求已经注册到 scheduler，但尚未 pending。当前请求的 Init 不能直接增加已经结束的目标 prefill 时间。
- Query 在 `chat_generation.cpp:740`、提交 scheduler 之前且锁外执行；Append 在 step 返回并提交接受前缀之后执行。下一轮必须等它们完成，存在串行 CPU 成本。`chat_scheduler.cpp:155` 的条件变量等待和 `:369` 的多流 API 调用仍用于 ON/OFF；ready=false 传空 suffix span，没有零命中专属 GPU 工作或额外 GPU 同步。当前日志不能把 query 纳秒推算成 scheduler 唤醒延迟。
- 共享全局 token 属性表只在显式 ON 的 server Start 构造；248320 B、unknown=243，Decode 保留 special。10 个 ON 服务构造中位 19.349083 ms（19.076443–19.596426 ms），发生在 ready/首 HTTP 之前，不能直接加到请求 TTFT。表和请求索引对后续 cache/allocator/统一 RAM 的间接影响并未由 host 累计计时覆盖。
- `Elapsed` 用 steady_clock 测量各函数包围区间；numeric_ns 已包含在 query_ns。计时不是事件追踪，未独立测量时钟读、调用边界、cache 扰动、唤醒或环境间接效应。不能把其数值当严格 E2E 因果上界，也不能把未计项目记为零。

## 已封存成本与保护项的量级

124 条 ON 请求合计：7236 queries，1110 hits；Init 28.056652 ms、Append 5.444485 ms、Query 5.666892 ms、Release 0.719945 ms，索引包围区间合计 39.887974 ms。1961 次 numeric 检查覆盖 74812 个逻辑匹配 token，numeric 合计 1.230179 ms（已含于 Query）。这是全阶段合计，不是单条请求延迟或节省预测。

新 8K 的四组均 0 hits，ON/OFF 同组生成输出、接受分布及目标/draft 工作计数一致。下表每组为一个服务启动中的三请求；post-TTFT 差是 `median(latency-TTFT)_ON - median(latency-TTFT)_OFF`，不把相同工作计数推导成相同吞吐。

| head / 块 | post-TTFT 中位增加 ms | Init 范围 µs/请求 | Append+Query+Release 范围 µs/请求 | numeric 范围 µs/请求 |
|---|---:|---:|---:|---:|
| full / 1 | 5.766060 | 109.084–141.289 | 90.826–136.685 | 0.297–0.417 |
| full / 2 | 12.729267 | 115.733–117.965 | 71.375–96.439 | 0.472–0.602 |
| subset / 1 | 6.964830 | 87.898–106.380 | 60.986–102.518 | 0.371–0.639 |
| subset / 2 | 29.509306 | 110.306–116.696 | 71.834–88.868 | 0.407–0.538 |

以上 Append+Query+Release 包含首轮/最后释放，窗口比 post-TTFT 更宽，不能直接当其精确分解。每条新 8K 请求只发生一次数字检查，匹配长度 6、short_rejected=1，numeric_rejected=0。即使完全删除该数字检查的包围时间，量级也仅 0.000297–0.000639 ms；实际早停还保留部分工作。

| subset 场景 / 块 | TTFT 中位增加 ms | Init 范围 ms/请求 | Query 范围 µs/请求 | numeric 范围 µs/请求 |
|---|---:|---:|---:|---:|
| 45K / 1 | 25.800927 | 0.291659–0.634326 | 47.704–49.716 | 3.400–4.221 |
| 45K / 2 | 58.532078 | 0.329086–0.516764 | 33.783–41.479 | 3.288–4.433 |
| 200K / 1 | 265.454567 | 0.788178–0.877892 | 44.040–48.390 | 4.257–5.072 |
| 200K / 2 | 152.665700 | 0.785240–0.818591 | 36.095–44.551 | 5.037–6.073 |

这些是完整生成期间所有 Query/numeric 的合计，只有首轮属于 TTFT。全部 12 条均 0 hits。45K 每请求 11 次 numeric 检查/62 个 token；200K 为 12 次/68 个 token；都因 min32 拒绝，没有数字拒绝。

独立 runtime 审计按 exact response_id 对齐 handler `stage=decode`（目标 prefill 完成、当前 Init 尚未开始）。子集 45K B1 的三请求 TTFT 差合计 173.527 ms，之前该事件差合计 163.907 ms；B2 为 152.900/154.993 ms。200K B1 为 861.653/799.468 ms；B2 为 550.082/554.155 ms。大部分观测差已出现在当前 Init 之前；比例不是因果归因或总体统计估计，超过 100% 表示剩余阶段抵消。不能据此排除跨请求/服务级 allocation、cache、启动条件或环境影响。

普通 decode 的五档负向观测也不能由本候选改善来解释：OFF 不构属性表、不 Init/Query/Append 索引。不同 binary、公共路径或启动/环境因素须单独定位。两块内的小幅负向不是已证明的总体退化，也不能先验归为噪声；本记录不重开已结束 NO_GO 或更改其原结论。

## 唯一保留候选：数字检查的否定项短路

范围只限 `ProposeAfter:249`：按原顺序读取选中匹配段的 token 属性；一旦 `(flags & kSuffixAllowedAscii)==0`，停止读剩余属性并判定数字门不拒绝。继续执行原 min32。全段 allowed 时仍读到末尾，按是否含 digit 决定拒绝。

正确性推理：原拒绝条件是 `AND(allowed_i) && OR(digit_i)`；一项 allowed 为假后，后续任何 flags 都不能令此条件为真。因此不改变最长/最新选择、selected match_end/length、完整三候选、d0、numeric 优先于 short 的顺序、reason、ready 或三个全局 token。未知/empty 属性 0 同样使数字门不拒绝；不用子集映射。Init、Append、内存容量、pending 视图和 GPU 状态路径完全不需要变化。

计数合同必须明确：现有 `classified_tokens += match_length` 等于完整选中段长度。若保留该字段，应定义为逻辑检查跨度而非实际属性读取数；若需评价减少了几次读取，应另计实际读取数，并同步日志/审计工具，不伪造旧字段为实际工作量。numeric_checks/rejected、所有 proposal 字段及拒绝分布应保持；query_ns/numeric_ns 的变化只能实测。新增计数本身可能抵消如此微小的节省。

若后续明确实施，最小直接合同应在 HTTP 首测之后验证：首/中/末位置非allowed；flags=0/2/高位组合；全部allowed无数字、数字仅末尾；31-token 全数字必须仍是 numeric_rejected，31-token 非allowed 必须仍是 short_rejected，32/64-token 对应准入；空表；全局 ID>65535；前后 proposal/counter（除明确改口径字段）一致。采用完整扫描独立参考，不用同一早停函数构造预期。不需要新增阈值、动态策略、候选搜索或神经状态算法。

价值判断：该候选是可证明的局部冗余读取删除，但新 8K 的数字检查总量不到 1 µs/请求，子集 45K/200K 不到 7 µs/请求且大多数在首 token 之后。现有证据不足以把它当有实际保护收益的候选，更不能预测它修复 prefill 的 25–265 ms TTFT 差。建议暂不实施，先完成主驱动的一次有界诊断；没有新证据时停止，不扩展成其他参数或结构扫描。

## 不采用的捷径及合同影响

- 把三元组改成 32-token key，或按长 key 另建桶，会改变哈希碰撞和最近四候选的逐出/覆盖；可能找回旧算法没保留的长候选。即使最后仍要求32，不能保证相同 selected proposal、ready 或 tokens。这是候选选择算法变化。
- 在数字门之前按 min32 早退虽可保持 ready=false，却改变 numeric/short 优先级、reason 和计数。前轮已冻结该顺序，不把它称为全合同无损简化。
- 忽略“不像长匹配”的请求、不初始化索引或按历史命中率关闭，都缺少保全后续准入集合的证明，并引入策略变化。当前不建议。
- 调整匹配最小长度、扩大候选、继续收紧门、预计算额外历史属性前缀数组或并行调度均不在本单一候选内。

## 身份与可复核来源

当前 HEAD `ec1d5b7eaac400dd3ffb4a005b75d403e2a3e5cc`；下列五个生产文件与受测 runtime commit `cb0fa73c3271e6fbce2cd9d282838de9643d2a3b` 的 diff 为空。只做静态/已结束证据读取；没有执行新候选或合同。

| 文件 | SHA256 |
|---|---|
| `src/mtp/suffix_drafter.cpp` | `e7e202d6a412c4a1741cb9c9e74b45c426fa95496647af4d780dccf8341109cd` |
| `include/q4t/mtp/suffix_drafter.h` | `8dc18d67f5b5af9050f56ca88129ae94d30e84ba5d0873740ea923ae1555e49e` |
| `src/server/chat_generation.cpp` | `207c18fe799a8d9673860a22d19af7325f1047299af64e7fea6571c82d5d4abf` |
| `src/server/chat_scheduler.cpp` | `84db0d947b5a07ff4f16a3588abcddc9b19f5d68231f9f1905226b864a8a28e4` |
| `src/server/chat_server.cpp` | `b8b98be4c462b4aa2f7eed6727d50c3b252fc7de61e6bddb287969c6e78e0de0` |

原证据 BASE 为 `/home/rm01/models/dev/qwen4-thor/.q4t-work/suffix-admission-20261024`。本次重新核对 `analysis.json` SHA `4584ed7493f2fd9208047d76cf603cb4fab33ff31cfd6d5d0502045276a8cdf2`、`audit-costs.json` SHA `bcffcf07929824b79c67d50fe4f5feb321f8747b0ec4953a70ab6ff9282df69f`、`audit-costs-summary.json` SHA `d63b003372622ba16b2caf7e558d602e00a893678b8d58ee3b2953438a5c76a3`。

本次重新读回 `acceptance-v2/run/block-{1,2}-{full,subset}-{off,on}/server.log` 八个文件，与 analysis 中各自 server_log_sha256 全部相同；所有 analysis 所收录 suffix 行与对应原日志行逐字相同。OFF/ON 原值和计数由这些已封存日志/analysis 取得；没有覆盖或新增原始样本。原 archive seal SHA `7cef262f473d1b991efdec7cdb794683855281f77a2c133e4adfa1c4ac15f461`。

新 runtime 离线衍生材料：`/home/rm01/models/dev/qwen4-thor/.q4t-work/suffix-zero-hit-20261024/diagnosis-runtime-observations.json`，SHA `c5130c270ab5545583519920f12ab84b14f0e36e20bb121ed990c14408d9c376`，120 条 MTP 五档记录/60 对 ordinal 对齐。它是本次诊断材料，不是独立验收。
