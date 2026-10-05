# Offload 缓存状态、路由与阶段读取机制

更新：2026-10-06。固定机制研究已完成，结论为
`NEW_MECHANISM_FINDINGS_COMPLETE_RUNTIME_CANDIDATE_NOT_ADMITTED`。
本轮取得新的缓存继承、实际供给与阶段读取证据，但未定位一项已证明可减少
工作的运行时候选。旧性能 NO_GO、默认关闭和整体 54GB 未知保持。

## 范围与身份

承接 d8482e4，分支 `codex/offload-mechanism-20261006`。
运行时仍为 `6b5169355d7aa666653c3b7076676e64f697d313`，binary SHA256
`8447d8982ba705518be23f255ddf8dc402e4feb1dbe9f42536acd9634cbb3aa6`；
runner 为 `c4856f480542c12cedb7faf8b58e97e1478b80b8`。
本轮只改观测入口、离线分析和文档，无运行时改动或新构建。

[冻结计划](evidence/offload-mechanism-20261006/scope-plan.json) SHA256
`2fdd504c5bef648e882371b9be3fc50e2b98f2089576d5dc983d9997b367990a`。
8 个新服务、32 次真实 tools/evalscope HTTP；每服务依次执行
`[P,1024,1024,1024]`，各输出 256。S 的 P=1024，L 的 P=8193。
A/C 为 global/request/quiet=0/0/0 和 1/1/1。k0 是前驱，k1 为主端点，
k2/k3 各自为次端点，不合并或删作热身。旧 64 请求不拼入本批。

| 固定顺序 | 条件 | 观测 bundle |
|---|---|---|
| m01 | AS | off |
| m02 | AS | on |
| m03 | CS | on |
| m04 | CS | off |
| m05 | CL | off |
| m06 | CL | on |
| m07 | AL | on |
| m08 | AL | off |

on 同时启用既有 phase/cache 快照、residency timing 和 router trace；
off 全关。固定 16GiB host/cache、swap0、C256/L2配置16/mirror配置8、
max_open200、max_seq1/max_prefill8192/max_len262144。
精度/GEMM 不变，MTP、Phase D、chunk_order 关闭。各服务前定向冷准备，
组内不清缓存。trace 整服务上限128MiB，四个 on 总上限512MiB。
collector 另有15MiB device、60MiB pinned及复制/写盘/计时成本。

每个 cell 仅一个服务，不能估计稳定噪声、显著性或尾部。
16 对同条件同位置 on/off 的 TTFT 差为 −1.3621% 至 +2.8533%，
decode tps 差为 −1.7052% 至 +1.7767%。它们是整个观测 bundle 与
时序差异的描述，不是纯开销、噪声地板或可扣除的校正值。
首对 AS off/on 之间保留审计修复间隔，不能冒称组序完全平衡。

## 执行与验证

8 服务/32 HTTP 全部完成，输出、容量、路径、冷态、身份与清理合同通过。
16 条 on 请求强制检查 phase/trace 完整性，没有以静默关闭接受 HTTP 成功。
所有固定服务终态后才统一分析，未追加有利样本，没有重复 HTTP。

131 项合同分别在各自首批通过：100项工具、16项分析合成、6项绑定修复、
4项首行换行、5项加载后环境。三次真实离线审计失败全部保留：

1. 审计器把冻结的 Python 源文件误作 JSON 读取，m01 后停止；拆分哈希绑定
   与 JSON 解析。在同一份 m01 数据重审时暴露下一项。
2. 首行摘要计算剥离了冻结摘要包含的 LF；定点修复后原 m01 重审通过。
3. m02 审计未预期 slot 模型加载后自动设置 `Q4T_MOE_STREAMS=1`。
   旧运行时源码及 off/on 服务日志均证明该行为，严格加入这一项后原数据通过。

原失败、三份限定恢复计划及每次退出状态保留；共有19个物理 run/audit
尝试，对应16个成功逻辑步骤。m01 最终用 r3 审计，m02–m08 用 r4。
旧质量11题、C++15项、真实权重数值3项按身份和原覆盖限定复用；
不能把它们称作新观测配置的全质量/全数值证明。新32条验证其输出合同。

## 已获得的新机制证据

12 次上一请求 inference_end 到下一请求 prefill_begin 的完整记录元数据
精确相等，证实记录到的软件缓存状态跨请求延续；不覆盖 payload、OS
page cache 或 in-flight 状态。92 对同长度请求的 ordered top-k ID 摘要
均相同，同 k 的 S/L 和 A/C 输入与输出摘要也一致。因此本批加载差异不是
由路由需求条目变化解释；trace 不含 router 权重，不证明所有激活相等。

16 条观测有80个相邻阶段区间，全部满足 lookups=hits+misses、
loads=misses、loads=L2hits+mirrorhits+readmisses。L2 hits 全为0，
mirror 有实际命中；这不证明可删除 L2 staging 或扩大 L2 有益。
8193 前驱的真实 prefill 为8192行加1行，最后一行仍归 prefill。
完整 decode 为255个 forward，32→255尾段保留，占记录 decode 时间的
81.09%–84.68%；累计/嵌套/并行计时不相加成独占等待。

下表只比较同 arm、同 k 的 L−S，均来自 on 组。
百分比为相对 S 的 HTTP 差，GB 为十进制；不外推稳定退化幅度。

| arm / 位置 | TTFT差 | decode tps差 | prefill加载差 | decode加载差 |
|---|---:|---:|---:|---:|
| A / k1 | +2.2942% | −1.9851% | +454 | +227 |
| C / k1 | +3.0994% | −4.0512% | +411 | +129 |
| A / k2 | −6.9984% | +1.1624% | +18 | +308 |
| C / k2 | −7.9490% | +1.1643% | +36 | +239 |
| A / k3 | +9.1857% | −2.2022% | +4 | +114 |
| C / k3 | +12.1017% | −3.4495% | +8 | +102 |

k1 decode 中，mirror 命中 A/C 少470/452次，实际 read miss 因而多
697/581次，软件读多1.927/1.606GB。GPU加载增加不足以单独解释增读。
差异延续到32→255尾段：C 的 GPU loads 反而少59次，mirror hits
少428次，read miss 多369次，PID存储读多1.869GB、elapsed多0.989秒。
这排除了“所有较慢尾段都只是 GPU 加载次数增加”的解释。

k2 的 prefill/decode 软件读及 GPU loads 均增加，PID读却均减少，
TTFT/decode时间也缩短。这一方向反转完整保留，既不是稳定加速证明，
也不能据此前驱标签或软件工作量单调推断时间。

k3 两阶段软件读总差 A/C 仅58.061/55.296MB，PID存储读总差却为
7.624/7.825GB。prefill 软件读差仅8.294/30.413MB，PID读差却为
5.579/5.563GB。PID包含进程所有文件存储读取，软件计数包含可能被
page cache满足的请求字节；不能据此锁定 readahead、SSD独占等待或
专家文件物理读的因果份额。HTTP首token边界与服务端phase边界分列。

k3入口全部48层GPU物理slot映射仍不同，成员集A/C有16/15层不同；
共同专家相对age差已为0，但结束成员集仍有14/13层不同。三次短请求
没有完全消除历史；raw tick及slot位置差不能直接当成重载次数。

8193前驱单独报告：C/A 的 TTFT 分别26.791/37.388秒，decode为
6.980/6.912 tps；prefill loads为37128/71471，软件读为102.133/195.432GB，
PID读为70.049/73.307GB。同需求下两种规划确有工作量差，仍是单次
观测，不能当成额外优化已通过或推广到所有长上下文。

## Prefill 空间与下一步

按当前 GPU slot 路径，phase入口不在GPU、但本phase要用的每个 distinct
(layer, expert) 至少加载一次。这是入口缺失需求下界，不是容量约束下
的可达最优。1K探针（k1–k3）prefill实际加载比该下界多6036–6097次，
占实际加载47.7%–49.5%；decode多2334–3113次。这些只是潜在可避免加载量的宽上界，不能换算
为已证明冗余、可实现加载降幅或秒数收益。入口已有专家可能在首次使用
前被逐出，容量与 needed 集合也可能迫使重载。

k1 prefill 的 L−S 实际加载差A/C为454/411，而入口下界差为448/440；
差的大部分与不同入口成员的必需首载同量级，不能全部称作可消除抖动。
本轮没有因“缺exclusive timing”一概否决减少实际工作的候选；具体缺口
是尚未证明哪一项改法能消除哪些加载。

下一阶段最窄任务是用已采 ordered routes、真实入口slot/age与源码做
离线GPU缓存回放：先复现现行分块、完整needed集合、reserved及按物理
slot平票的规则，核每phase计数和末态；复现成功后才比较替换策略。
稳定expert-ID平票仅是假设，可能不减少加载，不在此选中。
若基线无法复现，先修回放合同；若候选不减工作或破坏容量/保护合同，
则拒绝。无需先重复这32条HTTP。

L2/mirror策略仍缺逐miss实际来源、victim身份及worker次序；若选这条
方向，应另冻窄观测，继续采相同聚合数不足以填缺口。只有讨论关键路径
等待收益时，才需要进一步互斥依赖span。任何新运行时候选均先另冻
HTTP首测、受影响数值、完整五档加目标容量/history/质量/资源验收。

## 资源与证明边界

32请求窗、8服务、24原始流的资源分析及独立复核完成，128个IO夹逼区间
有效。cgroup I/O与PID没有一个窗口完全相等；模型分区259:1包含背景
活动，不能归因给q4t，也不与父设备259:0相加。
PSI全缺失，1724次GPU计划跳采、末两条PID IO未知均保留，后两条在
最后请求包络外。live cache峰未知；0 OOM及cgroup swap不等于无压力。

| 服务 | 终态memory.peak超16GiB (B) | 采样current峰超16GiB (B) | NVIDIA采样峰 (B) |
|---|---:|---:|---:|
| m01 | 0 | 0 | 55700357120 |
| m02 | 4096 | 167936 | 55717134336 |
| m03 | 0 | 0 | 55717134336 |
| m04 | 0 | 0 | 55700357120 |
| m05 | 0 | 0 | 55798923264 |
| m06 | 4096 | 0 | 55815700480 |
| m07 | 0 | 200704 | 55815700480 |
| m08 | 0 | 0 | 55798923264 |

current可能高于保留的终态peak，原值不抹平。NVIDIA是驱动口径，
不是整体物理并集，不与cgroup等计数相加，整体54GB继续INDETERMINATE。

最终保护通过：MAIN七项dirty路径、HEAD、diff和主binary与入口一致；
模型228文件清单/大小/inode/device/mtime一致，只哈希config/index，
未哈希权重。元数据相等不证明payload逐字节相等或无瞬时写入。
reference仅核tracked Git状态，无全树入口清单。八个自有unit/cgroup、
记录PID/PGID及8184监听清理通过，不宣称全设备GPU idle或所有隐藏进程
均不存在。工具退出与清理、独立机制/资源/最终审查均有独立记录。

## 交付材料

[紧凑结果与来源摘要](evidence/offload-mechanism-20261006/compact-results.json)
保存本轮比较、资源口径及验证范围，不包含私有请求文本。

原始证据位于 `.q4t-work/offload-mechanism-20261006/`，大报告/trace及
私有请求内容不进入Git。关键文件为 `mechanism-analysis.json`、
`resource-audit.json`、`mechanism-summary.json`、`mechanism-interpretation.json`、
`diagnostic-decision.json`、`final-protection.json` 和各独立review。
机制/资源报告SHA分别为：

- `b42e5c47d117a3ca8ad7ebc8b55e5259b92a2aecb69c732e7449f0a1e71bf9eb`
- `333e70323edc444ed866266a33d71453a7064808b5dcc7ff06256fb595b400b9`

原执行计划SHA为 `7170959e8084bbda642ffa8b37bdaf5de1b26d29e7cf028989a8c374aa1fb3c1`，
最终限定恢复计划SHA为 `2d1487b7559e34cff2d5bcae4e48d82a122ca82b1024901bd0705d61d2dcbf17`。
独立总审SHA为 `988dbc1c92d4010b76d0fa6c57f02ffbaed36b81cc100e49b449f735e407b2ca`。

195.8MB机制报告超过两个最终检查器的原128MiB文件界限；首次执行前
仅为该精确路径增至256MiB，其他文件仍128MiB，指标与接受条件未改。
原脚本/计划及审查保留，completion另严格绑定修正版protection身份。
这两处静态修正不计作新增执行失败，也不新增模型测试。

最终文档HEAD/远端SHA由 `delivery-audit.json` 绑定，
`completion-checklist.json`及`final-goal-audit.json`记录收尾核对。
交付在独立分支，不纳入MAIN的七项既有修改，不把commit/push或诊断
PASS当成运行时性能、整体质量或资源接受。
