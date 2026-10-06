# Mirror 保留历史 — 共同缺失需求上的供给差仍存在

更新：2026-10-06。固定八请求离线研究完成，45项新合同及一次实际trace
分析均首批通过，无新增HTTP、模型运行或runtime改动。工具提交为
`a2fdcfda40dbdc3d422542e9ee8ce48a8aa364bc`，分支为
`codex/offload-mirror-retention-20261006`。

主比较k1在输入token序列、ordered路由相同的条件下，两边都缺失的专家
仍有**至少10次净mirror入口候选差**。因此，全部470次候选差不能仅用
“当期缺失的专家不同”解释；并不说明470次全是保留问题，也未确认某次
GPU冗余副本挤掉了有用镜像。按冻结门只提名一项未来候选，未实施优化。

## 冻结问题与源码边界

[范围计划](evidence/offload-mirror-retention-20261006/scope-plan.json)
SHA256为`7b611ebf65f194f166d8d9e1635d88f5eced5e790d8fe38012fb754beff7e089`。
只复用上一阶段`s02-as-on`与`s03-al-on`各k0–k3，共8请求、48层、255个
true decode forward，共97920个层计划。AS输入为四次1024，AL首条8193、
随后三次1024；每条输出256。保留全部位置/层，旧16请求不池化。

被采样runtime仍为75d26ff、binary72a321a4；本次只是新分析工具。上一轮
27HTTP/资源与87工具合同的通过结论保留原覆盖范围，不当成本轮新增通过。
旧GPU回放核心、供给与observer合同按未变代码身份复用。

待检验机制：mirror保留了GPU/L2已有的副本，可能在写回时覆盖仍有独立
供给价值的镜像。代码实际查找顺序是L2→mirror→READ，而非header旧注释
中的先ring后L2；命中mirror不消费映射、不刷新cursor。写回从cursor
顺扫首个event就绪且未claimed的槽，不优先空槽或冗余副本。

真正单token forward入口同步既有stream；大prefill内部singleton子块
不能套用该前提。跨task claim/reserve、当前plan新event就绪情况与
publication顺序仍会改变实际ring状态，GPU规划顺序不等于写回顺序。
完整源码依据见[source-contract](evidence/offload-mirror-retention-20261006/source-contract.json)。

## 方法：需求可复现，mirror历史不作虚构

从每条实际decode入口初始化GPU状态，完整消费trace至EOF，跳过prefill
的策略回放，但核对其帧与输入行数。逐plan恢复missing/victim，核对
decode 1/8/32/255前缀计数、逐层source/clock及完整物理GPU末态。
每条独立使用实际入口，不声称模拟了反事实跨请求缓存链。

潜在mirror供给集合为“实际decode入口mirror ∪ 严格更早的GPU victims”；
不裁成最后8次驱逐。距离以plan数或严格处于两端plan之间的victim次数
表示，不含两端plan内部未知顺序，不能称实际mirror寿命或时间。

对同一层/步，GPU miss拆为共同、S独有和L独有。各分区潜在候选数每plan
最多8，累计为上界U。给定实际累计候选C，共同miss上的真实候选数X满足：

```text
X_common ∈ [max(0, C − U_only), min(C, U_common)]
共同候选差(S−L) ∈ [S.lower − L.upper, S.upper − L.lower]
```

每层检查C不超过总潜在容量、区间非空，再加总48层的区间。这是可行集合
的保守外包络，不保证区间内每点都能实现，也不提供互斥的因果份额。
输入token侧车摘要、长度和ordered路由均核对；同长度不替代输入身份。

## 主结果与全部位置

| 位置 | 输入token/路由相同 | 共同GPU miss | S独有 / L独有 miss | 实测候选差 S−L | 共同候选差的保守区间 |
|---|---|---:|---:|---:|---:|
| k0 | 否 / 否 | 1541 | 5587 / 5655 | 170 | [−124, 305] |
| k1 | 是 / 是 | 6529 | 471 / 698 | 470 | **[10, 845]** |
| k2 | 是 / 是 | 6359 | 200 / 508 | −272 | [−447, 214] |
| k3 | 是 / 是 | 6270 | 97 / 211 | −94 | [−188, 111] |

k0输入不同，只作描述。k2/k3反转保留，其共同候选区间跨零，不能据此
确认方向。k1实际C为S950/L480；S独有miss中最多460次有潜在mirror支持，
故S共同候选至少490，而L全部候选仅480，净差至少10。逐层约束给上界845。
上界可大于470，是因为独有miss贡献可以为负；不能把区间当470的百分比分割。

本批总GPU miss为54825，潜在来源支持23316次，按每plan容量截断后上界
23310，实际候选4482次。潜在集合没有实际槽可用性和完整替换顺序，差额
不是可消除READ，也不是本候选的节省上限。

k1中，距最近一次GPU victim的严格中间victim数为0–7的潜在miss，S有987、
L有501次。这说明近距离再需求机会本身不同；它们不是实际mirror命中，
不能把486次差直接归为造成470次候选差的原因。完整距离直方图与各层
区间见[结果摘要](evidence/offload-mirror-retention-20261006/retention-summary.json)。

## 端点冗余：存在，但未证明实际挤占

对24个完整快照、1152个层端点，按GPU-only、L2-only、both、sole、空槽
互斥分类。所有捕获端点的L2-only和both均为零；这不能外推到未捕获时刻。
下表每个端点均为48层合计384个mirror槽；这里“冗余”专指GPU已有副本。

| 序列/位置 | decode入口GPU冗余 | 入口sole | decode末端GPU冗余 | 末端sole |
|---|---:|---:|---:|---:|
| AS k0 | 3 | 381 | 11 | 373 |
| AS k1 | 2 | 382 | 27 | 357 |
| AS k2 | 2 | 382 | 20 | 364 |
| AS k3 | 2 | 382 | 17 | 367 |
| AL k0 | 5 | 379 | 11 | 373 |
| AL k1 | 6 | 378 | 12 | 372 |
| AL k2 | 2 | 382 | 24 | 360 |
| AL k3 | 2 | 382 | 18 | 366 |

k1的decode入口，AS层26/35、AL层6/15/21/26/46存在同ring的GPU冗余与
sole镜像共存，满足冻结门的端点必要条件。入口这些GPU冗余专家若在255步
内再次成为GPU miss，均先被GPU驱逐；仍不能证明原mirror副本保留到了那时。
请求交界重复捕获同一状态，端点统计不是独立样本，也不是物理RAM计量。

## 决定与下一步候选

阶段决定：`FUTURE_GPU_COVERED_MIRROR_RECYCLING_CANDIDATE_ONLY`。
主比较满足精确输入/路由相同、共同差下界为正，以及相关decode入口
同ring混合占位三个预先条件；因此只提名**优先回收GPU已有副本的mirror槽**
供下一阶段考虑。L2-only端点不用于触发这个GPU规则。

本阶段没有改变选槽、移除映射、增加CUDA调用或实施该候选；假设仍为
未识别，真实覆盖损失频次和可实现收益未知。实现必须使用运行时当下可得
且线程安全的成员信息，保留原claim/in-flight/event保护与fallback；
不能在并发worker里无保护读取正在变化的GPU映射，不能使用未来路由。

下一阶段可围绕这一条规则冻结默认关闭实现、HTTP首测及完整五档/目标/
history/资源验收。当前不自动追加观测，不试第二规则，不承诺prefill或
decode提速。旧NO_GO、默认关闭、精度/GEMM/预算及MTP/Phase D状态保持。

## 验证、证据与保护

26项核心加19项runner合同首批45/45通过，覆盖有限枚举、严格前序关系、
错误ID/尾部、输入身份、前缀/物理末态/source不闭合和候选门反例。输入
投影、一次真实分析均首过，所有自有进程退出码0并清理完成。首测前输入
token身份缺口及scope组装的相对路径预检错误保留，未靠追加数据修正结果。

输入投影SHA256：`f3831ccc88e061aea3141f23f581be9c963da827ee3ded7761509d501c73dcb3`。
执行账SHA256：`fd413fa124342029be9b9bd8c87cb7d76d420e8994bb40d87889cbb4fa0e797e`。
原结果SHA256：`bd91e5252d97ba4c1c975fc28f83d453f755588fde762e1df6c86416692d159c`。
完整decode计划SHA256：`f28cd33582ab47b97abda963933a82b5960ecf5e58343d888430e2f6856b4e83`。
31.55MB原结果、7.87MB计划与3.94MB输入保留本地R目录，Git只归档紧凑
结果、执行/审查证据及生成脚本；投影不替代完整原件。

[独立结果审查](evidence/offload-mirror-retention-20261006/result-independent-review.json)
通过：独立重算1152个端点分类、97920个计划上的机会、384份逐层记录及
直方图、3072个入口首次事件、32个前缀、48960份逐plan分区容量与192个
逐层区间，并核对摘要全部字段。未重新解析原trace或重跑GPU策略。
独审辅助比较曾因Counter丢弃零值键失败，已保留
[方法修正](evidence/offload-mirror-retention-20261006/result-reviewer-method-note.json)；
修正只补齐比较键集，未重读分析产物。独审不称首过，不与项目45项合同及
一次输入/分析首过混写。

[最终保护](evidence/offload-mirror-retention-20261006/final-protection.json)
首执行通过，其自身退出码0、无失败并清理完成。保护脚本首执行前补齐
入口两份基准固定SHA断言，静审发现及修正保留。MAIN原七项修改/完整diff
与主binary保持，旧两个交付工作树HEAD/干净状态保持。模型审计仅228项
元数据与config/index摘要，不读payload；元数据相同不能证明内容逐字节
相同或从未瞬时写入。reference只证明tracked Git状态。原trace身份以
已核SHA及未变stat复用，不排除同stat内容替换；退出证据只证明自有进程，
不声称整机GPU空闲。证据副本及范围见
[归档说明](evidence/offload-mirror-retention-20261006/README.md)。

最终提交仅文档/证据，复用已通过合同与分析。推送后的分支/远端、文档
差分与原工作区有限交付核对记录保留本地R/delivery-independent-review.json，
不为把该记录收入自身提交而重复模型或保护全套。无新资源测量，上一轮
PSI/GPU跳采/物理计量缺口继续保留，整体54GB仍未知。
