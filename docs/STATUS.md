# 当前状态

更新：2026-10-04。代码是实现事实来源；验收规则见 [EVALUATION.md](EVALUATION.md)。
本入口仅维护当前决策，过程记录见当天日志与专题报告。

## 当前结论


## Offload分区运行时：完整性能NO_GO（2026-10-04）

详见[分区运行时验证](OFFLOAD_PARTITION_RUNTIME_2026-10-03.md)。
min_new_csr_v1已接入，默认关闭、严格开关、超预算整forward回退；
chunk_order仍0，GEMM/精度冻结，MTP及Phase D关闭。干净源码26ca438
构建7ce25d74零警告，首测HTTP固定11题、133项host/工具及两项真实
权重数值合同首次通过。BF16和所采FP32逐位相同，覆盖singleton/
非连续回填；只证明冻结层/形状，不外推全部中间值或全部任务质量。

45K初筛通过后，16GiB同binary完整六档off/on各3次于10月3日23:47收尾。
36请求输出/容量/身份及清理通过；48层9504次候选prefill、零回退。
全部TTFT门槛通过，但1K/4K/8K最低decode分别下降3.63%/0.45%/
0.076%，完整性能有效NO_GO。45K/200K/261887三档通过；长档平均
TTFT减少28.81%/31.20%/31.11%，不能抵消短档失败。按冻结出口拒绝
当前全范围策略，业务/生命周期条件未触发，无有利重跑或门槛放宽。

四组资源与42条请求日志首次审计完成。长档后两次GPU补载约减半，
200K/261887的PID存储读量三次均值却增加3.29%/3.54%；软件nvme_mb
不能替代物理读取。cgroup I/O低计并平台，livecache/PSI未知。
完整off/on内存计费峰比16GiB设置高729088/765952B，OOM/swap为0；
NVIDIA计数峰56.237GB不可当去重物理并集，54GB继续INDETERMINATE。
原7处修改独立保留；候选保持默认off，下一步优先短档阶段归属与缓存
状态诊断、整体RAM去重测量，不直接启用或改GEMM。

## Offload有界实验收敛：重排NO_GO，分区离线GO（2026-10-03）

详见[本轮完整结果](OFFLOAD_AUTONOMOUS_2026-10-03.md)。干净binary
6ab2a820固定质量11/11通过；16GiB同binary off/on六档各3次全部
输出/容量/身份通过且正常收尾。重排在1K、4K、261887档有冻结
速度门槛未过，完整性能NO_GO；长档平均TTFT改善约3.3%–3.8%，
不能抵消未通过项。Q4T_MOE_CHUNK_ORDER默认0，真实D验证未触发。

唯一后备min_new_csr_v1按冻结范围完成一次96层离线研究：两个
首8192-token forward的GPU补载减少48.49%/47.25%，结构下界
下降、块数下降、无回退。仅OFFLINE_GO_FOR_CONSIDERATION，
这是接入前的离线结论；随后真实路径验证已完成，见上节。单token
子块会改变dispatch，离线结果本身不提供数值/HTTP/SSD/TTFT接受。

133项host/工具检查通过，构建零警告；C++行宽整理后两个helper
二进制与实验版本逐字节相同，复用已通过证据，无重复模型/trace。
四组原始资源审计完成，42请求可夹逼计数；cgroup I/O低计并平台，
PID/分区读量相互支持但归属分列。完整off/on的memory.peak比
16GiB设置高864,256/880,640B，OOM/swap为0。NVIDIA峰56.237GB
不是整体物理并集，livecache/PSI未知；54GB仍INDETERMINATE。

原7处未提交修改独立保留，模型/reference只读，GEMM冻结、MTP与
运行时Phase D关闭；最终证据/工作分支交付身份见本轮报告。

## Offload离线回放 Goal完成（2026-10-02 22:31）

结果见[实际时序回放](OFFLOAD_REPLAY_2026-10-02.md)。58项工具合同通过，
8组冻结比较完成；运行时/binary46b3f977不变，已有HTTP证据复用。
当前45K真实日志的原分块顺序下至少382732次prefill补载不可避免，
单改驱逐的可避免上限仅7.0%–7.2%；固定各forward入口的局部理想
驱逐仅再少0.38%–0.40%，后者不能当全请求最优。

唯一重排候选在主45K及业务留出减少5.9%–7.0% GPU补载，decode计数
未增；主样本baseline前两次GPU计数与真实日志精确一致。L2/mirror
仍是条件排程；理想对象缓存缺失量改善仅0.2%–1.5%，不预测SSD/速度。
热点逐ID可由32条calibration重建，业务留出独立；旧chat holdout
已被后来校准集使用，不能再称当前热点留出。

建议保留重排作下一阶段唯一有界HTTP小步候选，不据此承诺TTFT−30%；
更大收益需研究分区本身。54GB整体RAM仍未通过，16GiB只为研究条件。
GEMM冻结、Phase D off，原7处未提交内容完整保留。

## Offload离线回放 Goal启动（2026-10-02 22:06）

范围见[实际时序回放](OFFLOAD_REPLAY_2026-10-02.md)。保持线上binary
46b3f977及GEMM/Phase D状态不变，复用已通过的HTTP；补逐token、
真实分块与分层缓存条件回放，冻结一个子块重排候选与固定分块理想值。
54GB整体目标未通过，16GiB仅作host/cache研究条件。已有trace缺少
worker/event时序，L2/mirror显式列模拟假设；不冒称物理IO或性能验收。

## RAM/缓存 Goal：预算修复与有界对照完成（2026-10-02 20:40）

完整结果与后续建议见[RAM/缓存协议](OFFLOAD_RAM_PROTOCOL_2026-10-02.md)。
新工作区binary `46b3f977`零警告；budget host/sanitizer各13/13，
工具45/45、原验收工具30/30。1%预算启动在model_load前拒绝；固定
HTTP质量11/11（含200K），两组45056×3输出均同参考，容量262144不缩减。

不限host/cache组TTFT 135.8/131.8/131.6s、decode9.82–9.97；16GiB
组223.1/224.1/224.3s、7.25–7.31。PID存储读取分别27.906/0.769/
0.174GB和438.850/441.757/441.614GB；逻辑读取均约1.14TB/请求。
这是局部资源对照，未接受性能策略、未跑完整五档+目标档矩阵。

冷态不限组memcg峰70.153GB；16GiB组峰17,179,885,568B（比设置高
16KiB），两组swap/OOM均0。CUDA未完整计入cg，整体物理RAM仍未知，
54GB目标未签PASS。受限cg io.stat严重低计（第三发仅0.223GB），
PID与分区设备读量相互支持，具体内核归属原因UNKNOWN；三口径分列。

三服务正常退出、unit清理、模型文件与binary身份均核实。原Phase D/
统计/日志未提交内容继续保留，工作区HTTP不等于干净提交树发布资格。
下一步先定部署预算，按真实分层/分块时序离线评估prefill重复补载收益
上界，再选一项在线策略；GEMM冻结、Phase D off。

## RAM/缓存协议阶段：隔离校准完成，预算待冻结（2026-10-02 19:20）

用户已允许启动下一阶段，计划见
[RAM预算与缓存协议](OFFLOAD_RAM_PROTOCOL_2026-10-02.md)。本轮只做
14阶段×3的小探针，无模型推理、无运行时代码修改、无模型cache清理。
独立1GiB transient服务内，CUDA device增加64MiB时memcg current
中位增量为0，而NVIDIA增加64MiB、KReclaimable减少同量；pinned和
普通匿名页进入计费。外部预热32MiB cache的读取也不新增file计费。
因此memory.max不能直接当q4t全部物理RAM上限。

探针exit0、无OOM，服务/cgroup/任务文件已清理。下一步分别核设备/
主机/cache覆盖，补有界HTTP与实际存储IO采样，处理预算无解仍回退
启动、空热点层计费规则漂移等已发现边界，再冻结小规模HTTP协议。
建议先探索C=256可运行资源范围；54GB是否为最终目标待选择，不
自行恢复68.34GB旧口径或宣称新的总物理峰值。GEMM冻结、Phase D off。

## Offload 基础修复 Goal：修复与有界验证完成（2026-10-02）

交付、身份与边界见 [修复报告](MOE_OFFLOAD_REPAIR_2026-10-02.md)。
3b关闭路径、部分短读、分片读期所有权已修复；完整工作区binary
`dc845dd3` 构建零警告，相关集成11/11、固定HTTP质量11/11（含200K）、
merge关闭1K/8K、故障及prefill/decode取消恢复全过。IO host与
ASan/UBSan各7/7；验收工具30/30、内存工具9/9。

逐文件账已核实：三组模型文件cache窗口峰73.58–73.95GB，NVIDIA
计数峰56.24GB、RSS峰约4.70GB；这些分项峰值不可相加。总物理峰值
仍为INDETERMINATE，旧68.34GB不得当作含缓存总峰值。两组未采到
退出端点及4次GPU查询空值原样保留；服务退出码均0。canonical工具
已修监控收尾竞争，并独立覆盖C3预热probe/矩阵进程。

阶段提交包含修复所必需的入口3b管线；Phase D及原统计/日志改动
保留在工作区。隔离3b提交候选独立编译零警告、residency8/8，
其源码身份与含关闭Phase D的HTTP工作区不同，不冒称同一发布身份。
本轮未重做性能矩阵，不宣称60% decode、TTFT降低30%或性能保持
达标。GEMM冻结、Phase D off；下一步先定实际RAM/缓存协议，再选优化。
下方均为历史阶段快照，当前结论以上述记录为准。

## Offload 诊断纠正：新候选合同缺陷与内存测量缺口（2026-10-02 16:47）

全面审查见 [offload 诊断](MOE_OFFLOAD_DIAGNOSIS_2026-10-02.md)。本轮
不改运行时代码，不启动模型测试；在途 d0 保持运行。以下纠正优先于
下方历史阶段说明：

- ed68cd3d 的六档 53.0%–58.1% decode、目标容量与所测 HTTP 输出
  一致性证据保留；当前 e5b7c782/未提交 3b+Phase D 不是该已验收身份。
- **68.34 GB 不能认定为含模型页缓存的总物理峰值**：门禁只加
  post-matrix−post-load 的 7.286 GB，漏掉加载期约 59.06 GB 的全机
  Cached 增长，并使用服务退出后的末态。准确总峰值须核归属、重叠
  与同时点；用户接受 C=256 超支的授权保留，数值口径待纠正。
- 新 3b 的关闭开关错误构造任务索引（host 最小复现已确认），
  ReadRangev 的部分短读重试未前移目标缓冲；另有分片缓存驱逐时的
  读期所有权风险。均未修复，不能把 3b=0 宣称为安全回退。
- 方法纠正：约 70% 相邻 overlap 不能证明 LRU 是重读主因；
  17.28 倍来自 45K 预热，目标档约 117.6 倍；nvme_mb 含页缓存命中。
  Phase D 按 forward 累加整批权重，与 token 时间尺度不同。
  capacity_curve 的留出 static/hybrid 使用本 split 选热点，不能
  作为部署预测；实际热点生成仅用 calibration 的证据仍成立。
- 建议下一步：成组修合同和验收脚本 → 核清内存/流量 → 复现真实
  分区与跨阶段缓存的离线模拟 → 选一个有收益上界的 prefill 改动。
  当前镜像写回的低复用值得优先检验；Phase D 维持 off、GEMM 冻结。

## 新目标：四项内存中性 decode/TTFT 优化；Phase A/B 完成，Phase C 在途（2026-10-02 13:00）

用户设定新目标（2026-10-02）：在已验收最终候选（ed68cd3d，B=12288，
C=256 每层命中 top-n/L2-16/K=8/max-open-shards=200）基础上推进四项内存
中性优化：(1) 可观测性（per-request residency 行打印 mh/mw/msk + L2
decode/prefill 分列）；(2) 单 miss 快速路径（Q4T_MOE_INLINE_MISS_LIMIT）；
(3) prefill TTFT 三件套（sub-chunk 切分/相邻 pread 合并/投机补载，目标档
TTFT 937 s→≤656 s，重读因子 17.28→≤10）；(4) 驱逐策略权重优先（tick-LRU
基础上引入 router 权重/频率优先，45056 decode ≥+10%）。GEMM 冻结；改动
限定驻留层 + prefill 补载管线（moe.cu sub-chunk 切分/调度，不碰 GEMM
内核）；不得新增内存超支（峰值 ≤68.34 GB）；验收 = 六档 decode ≥60%
C=0 基线（当前 53–58%，50% 门槛不回退）+ 目标档 TTFT ≥30% 下降 +
bitexact + 质量/业务/生命周期。

Phase A（项 1）完成：per-request residency 行新增 mh/mw/msk + ld2*/lp2*
（L2 decode/prefill 分列），compare_e2e.py 解析扩展；纯打印不改计算；
bitexact B=12288（base C=0 vs candfinal）+ C=0 跨二进制（旧 ed68cd3d vs
新）双档全过。
Phase B（项 2）完成：Q4T_MOE_INLINE_MISS_LIMIT（默认 1，clamp [0,2]，
0=回退）——LoadPhase1 chunk 条目数 ≤ 阈值时调用线程内联 stage+commit，
跳过常驻 load worker 的 mutex+cv 派发/等待往返；多 miss chunk 保留原流水
线。bitexact 全过；45056 定向×3 decode 9.98/10.05/10.08 tps（vs 候选
9.9721 仅 +0.7%，目标 ≥10.97 未达）、TTFT 133.1–133.4 s 持平、内存峰值
无新增超支（fixed=63.76 GB 含镜像项）。前提"phase1 屏障/调度开销为主要
来源"被数据否定（dphase1 由实际 pread+swizzle+H2D 主导，worker 派发往返
仅微秒级）→ 内联路径保留为可配置项（默认 1），不靠它追 +10%；decode 主
杠杆为项 4（驱逐策略降 miss 数）与项 3（TTFT 三件套）。

下一步：Phase C（TTFT 三件套）冻结假设/改动范围/出口 → 实现 → bitexact
→ 目标档+204800 定向×3（TTFT/重读因子/加载量/decode）。GEMM 冻结不变。

## 最终候选验收完成：容量/性能/正确性 PASS，内存 USER_APPROVED_OVERRUN（2026-10-02 08:55）

final-acceptance B=12288（二进制 ed68cd3d，C=256 每层命中 top-n/
L2-16/K=8/max-open-shards=200；C3 协议基线 C=0 与候选同节奏）
06:44–08:55 完成，冻结口径 compare_e2e：

- 容量：目标档 in=261887+out=257=总上下文 262144，基线/候选各 3/3
  finish=length，服务端 token 计数核对，无截断 → PASS
- 性能（decode，hmean，每档 3 次，首/后续分列）：1024=53.8%、4096=
  53.9%、8192=53.0%、45056=55.5%、204800=58.1%、261887=54.2% →
  六档全部 ≥50% 基线 → PASS
- 正确性：六档 bit-exact yes；质量 11 题 manifest-exact + 基线/候选
  逐字一致；业务 6 项 bit-identical（8243/45107/204851）；生命周期
  （补载失败/取消/槽位复用/跨请求）verify-c1 六查 K=8 PASS → PASS
- 内存：候选 rss+gpu 峰值 61.056 GB + 模型页缓存增量 7.286 GB =
  68.34 GB vs 54 GB → USER_APPROVED_OVERRUN（超 14.34 GB，用户
  2026-10-01 07:25 授权 C=256 超支；记录实测峰值，非放宽 PASS，
  严格 54 GB 口径未达成）；基线 C=0 峰值 91.40 GB（参考）；swap
  used 峰值 1.49 GB（未掩盖预算）

验收报告定稿：docs/MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md §6b；
部署/回退：docs/MOE_RESIDENCY_DEPLOY_2026-09-30.md（K=0 回退边界
C1+C2+C3 已验证；--moe-resident-slots 0 回全常驻逐位不变）。
后续项：per-request residency 行补 mirror_hits 打印（可观测性，
不重跑矩阵）。GEMM 冻结（用户 10-02：目标完成前不改，之后另行
讨论）。


## C6 max-open-shards 修复：45056 decode 10.05 tps = 56% 基线，超 50% 门槛；final-acceptance 在途（2026-10-02 05:30）

C6（驻留层，GEMM 冻结）：c5b 分段计时定位 decode miss 临界路径主因不是
pread 而是 shard reopen——每个 decode miss 做 ~15 次 EnsureOpen，32 槽
open-shard LRU 在调用之间把 shard munmap 驱逐再重开（open+mmap+200 KB
header 解析，~1.5 ms），dpread 5.4 ms/miss（c5b：dstage 5.4/dpread
5.4/dphase1 4.6 ms，decode 6.45 tps=36%）。修复：Q4T_MOE_MAX_OPEN_SHARDS
（默认 32，0=索引内全部分片）；验收配置 200 ≥ 197 分片，全部保持打开
（数据仍走 pread、无进程页错误，mmap 仅触碰 header，不重复 09-19
OpenAllShards 回归——那次是启动预开全部分片且数据走 mmap，14 worker
页错误争 VMA 锁）。C6 试点（C3 协议，45056×3）：decode
9.97/10.05/10.12 tps（基线 17.84，=56%，门槛 50%=8.9），TTFT 133.5 s，
dpread 0.49 ms/miss、dstage 0.71、dphase1 0.88；post-warmup 页缓存
68.7 GB（C5 保留生效），MemAvailable 65.5 GB。

C5+C6 已并入 C3 验收协议（c3-pagecache-protocol.sh 把
Q4T_MOE_MAX_OPEN_SHARDS=200 传播到探针服务器与验收矩阵，protocol.log
记录有效值；基线 C=0 不受影响）。

下一步：final-quality-business B=12288（质量 11 题 + 业务 6 项 bitexact，
新二进制 ed68cd3d 受影响项重验）→ PASS 后 final-acceptance B=12288 全
矩阵（基线 C=0 + 候选，五档 + 261887 目标档，每档 3 次，内存门
USER_APPROVED_OVERRUN 口径）→ 报告定稿。GEMM 冻结（用户 10-02 再确认：
目标完成前不改，之后另行讨论）。

## C5 页缓存保留修复生效（decode 4.86→6.45 tps）；decode 分段计时在途（2026-10-02 02:55）

C5（驻留层，GEMM 冻结）两件事：(1) **页缓存保留**：open-shard LRU 驱逐
专家分片时，SafetensorsFile 析构原本对整片发 POSIX_FADV_DONTNEED，把
C3 预热填的页缓存页全抹掉，导致每次按需专家 pread 回退 NVMe（~4 ms）。
新增 SetKeepPageCache(true, "-experts-") 让专家分片跳过 DONTNEED（非专家
分片照旧）。pilot-45056（修前）vs pilot-45056-c5（修后）：pread_avg
4.0→0.87 ms，decode 4.86→6.45 tps（基线 17.84，=36%，门槛 50%），
TTFT 220→136 s；meminfo 页缓存 post-warmup 4.65→68 GB（保留生效）。
(2) **decode 分段计时**（C5 诊断）：dstage/dpread/dphase1 计数器已接线
并打印到 [residency][timing]，用于隔离 decode miss 临界路径（stage vs
pread vs phase1/H2D）。pilot-45056-c5b 在途（~12 min）取数。

单测：safetensors_keep_page_cache 新增；Tegra 6.8 内核 mincore 对常规
文件返回 0（功能不可用），测试改为先探测 mincore、不可用则仅验证 API
路径并跳过驻留断言（端到端效果由 pilot 覆盖）。108/108 通过。

差距：6.45 tps = 36% < 50% 门槛（8.9 tps）。定量：decode 25.8
miss/token × ~3.8 ms/miss ≈ 99 ms/token 开销（基线 56 ms/token）。
待 c5b 分段数据定位主因：若 dpread 高（页缓存被请求期内存压力逐出）→
保页/降压；若 dphase1 高（H2D/commit）→ 优化 H2D 路径；若 dstage 高
（锁/排队）→ 降竞争。GEMM 冻结不变（用户 10-02 再确认）。

下一步：c5b 分段 → 定位 decode miss 临界路径主因 → 在驻留层内修复
（GEMM 不动）→ 重验受影响项（bitexact/定向）→ final-acceptance
B=12288 全矩阵。

## [D] K=8 六查 PASS、path-c 已合并+重建；final-acceptance 前 45056 试点在途（2026-10-02 00:40）

[D] verify-c1 K=8（最终候选 C1+C2+C3+C4）23:46:46 六查全 PASS（FAIL=0，
预算 fixed=63.76 GB 含 1.06 GB 镜像项，K=8 口径正确）；queue-post-verify
全链 business_rc=0 / vc_K0_rc=0 / vc_K8_rc=0。path-c 已合并主工作分支
（82bae2c，00:08）并重建（BUILD_RC=0，00:30，build/q4t
d08a44fad57a）。

用户 10-02 三项指示：(1) 目标档口径=总上下文 262144（256k），不是输入
256k；不再纠结 261887/261888 一字之差——现目标档 in=261887+out=257=
262144（finish=length）即满足，验收矩阵不改。(2) 即使内存预算超支，
支持 C=256 每层按命中次数 top-n——即 B=12288（hot-final-12288.json，
与 hot-256.json 同集合，已核验），保持 USER_APPROVED_OVERRUN 口径
（内存门记录实测峰值，性能/正确性门不放宽）。(3) GEMM 冻结再确认：
目标完成前不修改，之后另行讨论。

final-acceptance 前风险复核：r3 c256 矩阵（B=12288 同席位配置，无
C1/C2/C3/C4）各档 decode 22–28% 基线；verify-c1 定向 45056（C1+C2+C4）
仅 +2–3% vs r3 c256（4.84–4.90 vs 4.74–4.76 tps）；设计文档自述 C1
对单 miss decode 层收益小（stage 仍在临界路径）。定量：50% 门槛需
≤2.2 ms/miss（现 25.6 miss/token×~5.8 ms≈148 ms 开销）。先跑 15 min
试点（pilot-45056.sh：C3 协议 drop_caches→启动→45056 预热×8，候选
配置 C=256/L2=16/K=8/Q4T_RESIDENCY_TIMING=1，45056×3 SSE 计时），
实测冷/热 pread_avg 与 decode tps，再决定是否直接进 ~5h final-
acceptance B=12288；若试点 <50%，先定位 decode miss 临界路径再跑
长验收。

下一步：试点结果 → （≥50%）final-acceptance B=12288 全矩阵；（<50%）
按 timing 分段定位并修 decode miss 临界路径（驻留层内，GEMM 不动）
→ 重验受影响项 → final-acceptance。GEMM 冻结不变。

## [C] verify-c1 K=0 六查全 PASS；[D] K=8 最终候选六查在途（2026-10-01 23:40）

[C] K=0（回退边界 C1+C2+C3）23:07:56 六查全 PASS（rc=0）：fault
（修后武装成功）、cancel、定向 45056×3 全快于 r3 c256（decode
+1.1/+1.5/+1.4%，TTFT -6.8~-8.4%）；预算 fixed=62.69 GB（无镜像
项）。[D] K=8（最终候选 C1+C2+C3+C4）23:08 启动在途（~80 min）。
更正：20:28 首跑标签"K=8"有误，预算证明实为 K=0（fixed 无 1.06
GB 镜像项）；其 fault FAIL（脚本缺陷）与定向 run1 -0.1%（噪声）
已被 [C] 复跑取代，首跑证据不用于验收。环境传播已用
/proc/environ 实测核验（[D] 服务器 K=8、L2=16）。业务 12/12
逐位一致 PASS（[A]/[B]）。合并 dry-run 已验证无冲突（代码取
path-c、文档取主分支）。GEMM 冻结（用户再确认）。

下一步：[D] K=8 六查 → 合并 C1+C2+C4 + 重建 → 冻结最终候选 B
（R3_DECISION 算法）→ final-acceptance → 报告定稿。

## 业务 12/12 逐位一致 PASS；verify-c1 K=0 回退边界六查在途（2026-10-01 23:30）

queue-post-verify 在途：[A] 业务 nu15552+c256 重跑（修参后，r3
二进制 efce31d8）6/6+6/6 OK；[B] 对 base 逐位比对 12/12
bitexact=True → BUSINESS-COMPARE PASS（post-r3-affected 业务证据
链闭合：质量 3/3 + 业务 12/12）。[C] verify-c1 K=0（回退边界
C1+C2+C3）六查在途（~80 min），随后 [D] K=8（最终候选）。
verify-c1 首跑（K=8）FAIL=1 原因：fault 脚本 env 行截断（已修）
+ 定向 45056 run1 decode -0.1%（噪声级；TTFT 三跑 -6.4~-7.9%，
设计 C1 出口为 TTFT 对比；双跑复测）。文档已按 C1+C2+C3+C4
口径更新（验收报告 §6b、部署回退 §3 C4 条目）。合并范围确认：
代码取 path-c（4 commit）、文档取主分支；主 build 重建等 [A]
结束（已完成）后、双跑结束后进行。GEMM 冻结（用户再确认）。

下一步：[C] K=0 → [D] K=8 → 合并 C1+C2+C4 + 重建 → 冻结最终
候选 B（R3_DECISION 算法）→ final-acceptance → 报告定稿。

## C4 已实现，verify-c1 六查 4 过 / 1 脚本缺陷已修 / 1 在途；验收队列在途（2026-10-01 21:00）

C4 驱逐镜像已实现于 path-c 分支（wt-c1 887a3fe，107/107 单测，
含 residency_mirror_ring_hit）：每层 K=8 pinned 环（~1.06 GB，
Q4T_MOE_MIRROR_K=0 关闭=回退边界 C1+C2+C3），CommitExpert
stream-ordered D2H 写回，StageExpert miss 先查环再选 L2 victim。
verify-c1（K=8）在途：单测/bitexact/跨二进制恒等/cancel 四项
PASS；fault FAIL 为脚本 env 行截断（钩子未武装，已修）；定向
45056×3 在途。post-r3-affected：质量 3/3 PASS、业务 base 6/6
OK；业务 nu15552+c256 因旧版脚本缺 `--` 失败（已修，待重跑）。
queue-post-verify（setsid）在途：当前 verify-c1 结束后自动
业务重跑+逐位比对 → verify-c1 K=0 → verify-c1 K=8（PATH_C
验收顺序双跑）。GEMM 冻结（用户再确认：目标完成前不改，之后
另行讨论）。

下一步：跟踪队列 → C1+C2+C4 合并主分支 → 冻结最终候选（B 按
R3_DECISION 算法）→ final-acceptance → 报告定稿。

## r3 矩阵完成：两候选均 <50%（c256 22–28% / nu15552 28–37%），§5 分支 3 确认（2026-10-01 17:10）

nu15552 六档 3/3 全完成（17:06:33，rc=0，18/18）：
- c256（C=256 均匀+hot-256+L2-8）：decode 24.8/23.3/22.0/26.6/
  28.1/23.1% 基线，逐位一致 6/6，峰值 58.86 GB（授权超支）。
- nu15552（按层 top-n，C_l 256..446，总 15552，L2-8）：decode
  37.0/30.2/28.4/32.5/36.9/29.4% 基线，逐位一致 6/6，峰值
  68.07 GB（授权超支）；261887 目标档合同 3/3（in=261887
  out=257 finish=length，总 262144）。席位扩大使 261887 加载量
  2.89M→1.56M（−46%）、NVMe 7.99→4.31 GB/请求，decode 仅温和
  增益（+6–9pp）→ 瓶颈为稳态 miss 临界路径成本（6.2 ms/miss），
  非席位数量。
- §5 分支判定：**分支 3**（两候选均 <50%）→ C4（驱逐镜像）+ C3
  （页缓存预热）组合复测；主杠杆 C1+C2（miss 6.2→2 ms 时 C=256
  decode ≈54%）。auto-c1-verify 将自动写 section5-branch.txt 并
  跑 verify-c1 六查（C1+C2 二进制，含 45056 定向对比 r3-c256）。
- post-r3-affected 在途（17:06:33 启动：质量 11 题×3 组 + 业务
  6 请求×3 组，r3 二进制）。

下一步：跟踪 post-r3-affected → auto-c1-verify（分支 3 + verify-c1）
→ 合并 C1+C2 → 实施 C4（分支 3 触发）→ 冻结最终候选（C1+C2+C3+C4，
B 按 R3_DECISION 算法）→ final-acceptance → 报告定稿。GEMM 冻结
（约定持续）。

## nu15552 矩阵 204800 档 2/3 完成、请求 3 在途；final-acceptance B=12288 契约复核通过（2026-10-01 16:10）

矩阵在途（不触碰 GPU/页缓存）：
(1) 进度：nu15552 204800 档 2/3 完成（auto-12/13：in=204800 out=256
finish=length，loads≈1.207M、nvme≈3.33 GB/请求），请求 3 在途
（15:24 启动，~14.7 min/请求，ETA ~16:10）；261887 目标档（总上下文
262144）~16:10 启动，预计 ~17:30 完成。c256 六档已定（decode
22–28% 基线、逐位一致 6/6、rss+gpu 峰值 58.86 GB）。
(2) final-acceptance B=12288（C=256 每层 top-n，用户授权超支）契约
复核：final-acceptance.sh B 白名单含 12288，USER_APPROVED_OVERRUN
标志在位（内存门按用户授权口径记录实测峰值，性能/正确性门不放宽）；
hot-final-12288.json/cap-final-12288.json 在位（48×256=12288，15:30
已核验）。
(3) 自动链健康：auto-r3c (2621362) → queue-r3-final (2622739，
nu15552 在途) → auto-c1-verify (2639799，轮询 auto-r3c 退出后做
§5 分支判定 + verify-c1 六查)。GEMM 冻结（约定持续：目标完成前不
修改，之后另行讨论）。

下一步：跟踪矩阵 → compare 报告 → §5 分支 → post-r3-affected →
verify-c1 → 合并 C1+C2 → 冻结 B → final-acceptance → 报告定稿。
## 内存门页缓存口径改矩阵末态（含长档额外专家）；C3 协议补 meminfo-06（2026-10-01 16:05）

nu15552 矩阵在途（204800 请求 3，随后 261887 目标档 ~18:00）期间：
final-acceptance [4/4] 内存门原用预热页缓存增量（45056 预热），但验收
矩阵含 204800/261887 长档会加载额外专家 → 矩阵末态页缓存更大，54 GB
硬门用预热增量会低估。已修：C3 协议 --acceptance 矩阵后补
meminfo-06-post-matrix，evidence.json 增 page_cache_delta_matrix_kb
（矩阵末态稳态页缓存）；final-acceptance pcache() 优先矩阵末态、缺失
回退预热（兼容旧证据）并打印来源。模型加载瞬态页缓存不计稳态预算。
GEMM 冻结（第十一次确认）。

下一步：跟踪矩阵 → compare → §5 分支 → post-r3-affected → verify-c1 →
合并 C1+C2 → 冻结最终候选（含 C3）→ final-acceptance → 报告定稿。

## C3 协议补全 warm pread_avg 证据采集；自动链契约复核通过（2026-10-01 15:55）

nu15552 矩阵在途（204800 档 2/3，随后 261887 目标档 ~18:00）期间：
(1) C3 协议 --acceptance 分支补 Q4T_RESIDENCY_TIMING=1（基线/候选同设，
C=0 零开销）+ 矩阵后从 e2e server.log 提取 warm pread_avg 入
evidence.json，完成设计 §7.3 冷→暖证据对（原脚本仅采集冷侧，r3 矩阵
server.log 实测 0 timing 行）；(2) auto-r3c/auto-c1-verify/verify-c1/
queue-r3-final 全链 tag/正则/文件契约与 compare_e2e.py 实际输出逐行
核对一致。GEMM 冻结（第十一次确认）。

下一步：跟踪矩阵 → compare 报告 → §5 分支 → post-r3-affected →
verify-c1 → 合并 C1+C2 → 冻结最终候选（含 C3）→ final-acceptance →
报告定稿。

## nu15552 矩阵 4/6 档完成；部分分支读数：两候选均 <50%，§5 分支 3 大概率（2026-10-01 15:25）

nu15552（按层 top-n、DP 最优 C_l 256..446、总 15552 槽、≈68.8 GB
授权超支）15:24 完成 45056 档（3/3，逐位一致）；四档 decode
35–39%/30%/28%/32% 基线，全部 <50%。204800 在途（15:24:15 启动），
随后 261887 目标档（总上下文 262144），预计 ~20:00 完成。

部分分支读数：c256 六档 22–28%（已定）+ nu15552 四档 28–39% → §5
分支 3（C4 驱逐镜像 + C3 页缓存重测，或按失败维度报告差距）大概率。
席位扩大仅温和增益，确认瓶颈为稳态 miss 临界路径成本（6.2 ms/miss
有效），主杠杆为 C1+C2+C3（冻结依据：miss 6.2→2 ms 时 C=256
decode ≈54% 基线）。自动链 auto-r3c + auto-c1-verify 在途健康；
等待期间不触碰 GPU/页缓存。GEMM 冻结（第十一次确认）。

下一步：跟踪矩阵 → compare 报告 → §5 分支 → post-r3-affected →
verify-c1 → 合并 C1+C2 → 冻结最终候选（含 C3）→ final-acceptance →
报告定稿。

## final-acceptance.sh 契约修复（B=10752 白名单 + 页缓存精确字节）；r3 矩阵 c256 目标档 2/3（2026-10-01 14:45）

矩阵在途（c256 261887 目标档 2/3，ETA ~15:02；261887 档首请求已按合同
完成：in=261887 out=257 finish=length，总上下文 262144）完成 CPU 侧
契约修复：(1) final-acceptance.sh B 白名单补 10752（§5 分支 1/2 且
C≥224 时的条件候选，名单 14:32 已核验在位）；(2) 内存门页缓存增量改
精确字节（原 GiB×1e9 低约 6.9%，54 GB 门边界可能误判）。部署文档阶段
快照已提交推送（21d48eb）。c256 rss+gpu 峰值目前 58.84 GB（swap
1.10 GB，分列不掩盖），超预算为已授权测量。GEMM 冻结（第十一次确认）。

下一步：跟踪矩阵 → nu15552（cap=446 按层 top-n）→ post-r3-affected →
auto-c1-verify（§5 分支 + verify-c1 六查）→ 冻结 B → final-acceptance
→ 验收报告定稿。

## 最终候选 B 选择算法入决策包 + C3 协议 --acceptance L2 传播修复；r3 矩阵 261887 目标档在途（2026-10-01 14:20）

矩阵在途（c256 261887 目标档请求 2/3，ETA ~15:03；c256+L2-8 实测
rss+gpu 峰值目前 58.83 GB，超 54 GB 预算、用户已授权测量）完成：
(1) 最终候选 B 选择算法入 R3_DECISION 末节：B ∈ {7680,8448,9216,9984}
（§5 分支 1/2 且 C≥224 时追加 10752），按层 DP 最优 C_l（cap-final-<B>），
同一候选须同时满足内存（rss+gpu 峰值+模型页缓存增量 ≤54 GB，swap
分列不掩盖）、性能（六档 decode 调和均值 ≥50% 基线，每档 3 次、
首/后续分列）、正确性（bit-exact+质量+业务+受影响检查）三条，取
满足者中最大 B；无一满足则按失败维度报告差距，不放宽目标。
(2) 验收链契约核验：final-acceptance [4/4] 内存门口径与 ledger 一致；
261887 档基线/候选工作负载身份一致（输入 261887+输出 257、3 次、
无 warmup）；质量 11 题/业务 6 请求 fixture 与 wt-c1 二进制在位。
(3) C3 协议 --acceptance 分支 L2 未传播真缺陷修复（内存门 17.8 GB
虚高风险；C=0 基线不受影响；bash -n 通过）。

下一步：跟踪矩阵 → §5 分支 → 冻结 B → final-acceptance → 验收报告
定稿。GEMM 冻结（第十一次确认）；261887/261888 一字之差不再纠结，
目标档口径=总上下文 262144。

## C3 页缓存协议脚本就绪；C4 locality 证据入设计文档；r3 矩阵 c256 五档完成、261887 在途（2026-10-01 13:55）

矩阵在途（不触碰 GPU/页缓存）完成两项准备：C3 协议脚本
c3-pagecache-protocol.sh 落地（设计文档 §7：guard → drop_caches
冷基线 → 计时启动 → 45056×1 受控预热丢弃 → 页缓存增量/pread_avg/
重读因子证据 → 可选 --acceptance 同配置直连全矩阵；参数化
tag/C/hot-list/L2/binary，C=0 走全专家常驻基线路径）；guard 实测
矩阵在途正确拒绝。C4 locality 证据补入设计文档（主研究 20 请求：
再请求局部性仅 lag≤16 显著，lag32 0.94×/lag64 0.66× vs 随机 →
C4 收益温和 decode +3–5%，支持 C1/C2+C3 先行、C4 仅 §5 分支 3
时实施）。

r3 矩阵（13:52）：c256 五档 3/3 全过（decode 4.54/4.12/3.95/4.74/
4.14–5.21 tps，204800 TTFT 1166–1168 s），261887 目标档在途
（prefill layer 39/48）；随后 nu15552（cap=446 按层 top-n，用户
授权超支）→ post-r3-affected → auto-c1-verify/verify-c1。
GEMM 冻结（第十一次确认：目标完成前不修改，之后另行讨论）。

下一步：跟踪矩阵 → §5 分支决策 → C3 协议（最终候选 C 冻结后）→
最终验收。

## auto-c1-verify GATE 解析 bug 修复 + compare 加固 + 旧报告隔离；C1+C2 审查通过（2026-10-01 13:45）

矩阵在途（c256 204800 档 3/3）期间修复自动链隐患：auto-c1-verify 的
GATE 解析对 `ALL PASS` 只捕获 `ALL`（判定 `== 'PASS'` 永不成立，r2
全 FAIL 未暴露）→ 已归一化三态；compare_e2e.py 加固（zero-tps
ZeroDivisionError 根因、缺失文件→PENDING、均值用 len）并自测通过；
10:03 旧 traceback 报告隔离为 *-STALE-20261001T1003（新报告矩阵后
重新生成，缺失则安全停止）。C1+C2（df02f60）静态审查：C2 几何、
槽位唯一性、stream 顺序、错误路径、统计、内存账 4.68 GB 全部一致，
verify-c1 六查与 fixture 依赖核验，未发现正确性问题。GEMM 冻结
（第十一次确认）。

## GEMM 冻结（第十一次确认）：目标完成前不修改，之后另行讨论（2026-10-01 13:32）

用户：GEMM 暂时不修改，等当前目标完成后再讨论。记为第十一次确认，并新增
“目标完成后另行讨论”约定；期间任何改动均不触碰 GEMM 文件。git 复核：
GEMM 文件最后改动 2026-09-27（2782ef0），C1+C2（df02f60）至 HEAD 的 diff
无 GEMM 文件。r3 矩阵在途（c256 204800 档 1/3），不受影响。

## 隔离 10:04 无效 post-r3-affected（旧 auto-r3b 触发）；r3 矩阵在途（c256 204800 档 1/3）（2026-10-01 13:25）

旧 auto-r3b 观察器在第一次矩阵尝试失败后于 10:04 触发 post-r3-affected
（二进制 7f013d73，L2 修复前）：quality baseline 10/11 成功，第 11 条
（204800 token 提示）客户端挂起 ~353 s 且从未到达服务端（server.log 无
auto-10，shutdown 0 in-flight；服务端 30 s 读截止下到达必有痕迹）。同一
fixture 08:43 曾 11/11 成功；单次发生按瞬态处理。部分输出已隔离为
affected/r3-quality-base-INVALID-20261001T1004（必需：run_acceptance.py
拒绝非空输出目录，不隔离则真实 post-r3-affected（auto-r3c 3/3，~18:30）
会在 [1/4] 失败）。若再现，用 Q4T_ACCESS_LOG=1 harness 外复跑取 HTTP 痕迹。
矩阵（13:22）：c256 四档完成（decode ~24–28% 基线，与 r2 一致、优化前
预期内），204800 在途 1/3，随后 261887 → nu15552 → post-r3-affected →
auto-c1-verify（§5 分支 + verify-c1）。GEMM 冻结（第十次确认）。

## C1+C2 实现完成并提交（path-c 分支 df02f60，UNVERIFIED 阶段快照）；r3 矩阵在途（c256 五档中）（2026-10-01 13:05）

C1（stage→commit 流水线，worker 8→16）+C2（scale 4→1、gate/up
2→1，每专家 9 次 H2D→5 次）按冻结设计实现，提交
codex/moe-residency-path-c-20261001（df02f60）。并发安全审查通过
（plan 内专家/槽位唯一、commit 全部入单模型流、失败路径
ReleaseMissClaim、计数器 worker 局部合并）；wt-c1 独立构建零警告
（q4t ba7327a9），主树工作区已还原。GPU 验证按计划在 r3 矩阵报告
后进行（C1 验证配置 C=256+hot-256+L2-16，[q4t][budget] 逐项核对）。
r3 矩阵（13:04）：c256 四档过、204800 prefill 在途，随后 261887 →
nu15552（cap=446 按层 top-n，授权超支）→ post-r3-affected。
GEMM 冻结（第十次确认）。

## r3 逐 miss 计时完成；路径 C 设计冻结（C1 流水线/C2 H2D 合并/C3 页缓存/C4 驱逐镜像/C5 席位）（2026-10-01 12:35）

timing-collect-r3（efce31d8，C=256+hot-256+L2-8，45056+256）：
NVMe pread 2.40 ms/专家（页缓存 3.7 GB 冷读主导）、phase1
3.59 ms/chunk、d2h 3.12 ms/层、L2-8 命中 0、重读因子 17.5×
（1.19 TB/请求）、decode miss 25.7/token、TTFT 288 s（基线
30.55 s）。临界路径模型：decode ≈160–170 ms/token（实测
206–221），瓶颈 = NVMe stage 在 CPU 临界路径。否定结论：预取
上一步路由对 decode 无效（miss 专家必不在上一步 needed 集）。
路径 C 设计冻结（MOE_RESIDENCY_PATH_C_DESIGN_2026-10-01.md）：
C1 流水线 stage→commit+worker 8→16（先做）、C2 H2D 合并、
C3 页缓存策略（54 GB 含页缓存口径 → 最终候选 C≈160–224，
miss 单价降至 ~0.25 ms，decode 可至 ~95% 基线）、C4 驱逐镜像
（视 r3 结果）、C5 席位（r3 矩阵测量中）。决策逻辑三分支见
§5。GEMM 冻结（第九次确认）。

r3 矩阵 12:14 启动（auto-r3c，r3-cand-c256 → r3-cand-nu15552，
六档×3，基线复用 r2-baseline-s0），预计 ~22:00–23:00 出报告。

下一步：跟踪矩阵 → 报告后按 §5 决策 → 实施 C1+C2 → 定向验证。

## 第三轮矩阵首跑失败（L2 miss 竞态）→ 修复验证全过（efce31d8）→ auto-r3c 重跑链已启动（2026-10-01 12:10）

09:59 启动的第三轮矩阵两候选均在 ~1.5 min 内 rc=1（1024 档
out=28/46 提前 EOS）。根因：L2 miss 路径竞态——StageExpert miss
分支分配 L2 缓冲时未置 l2_claimed_，同 chunk 并发 miss 可经
PickL2Victim 选中该缓冲作 victim，NVMe 读未完成时覆写 payload →
输出损坏（L2-8/9 下 3/3 DIFF，L2-128 下 3/3 MATCH，与 r2 矩阵
bit-exact 6/6 一致）。修复（仅驻留层，GEMM 冻结第八次确认）：miss
路径同锁内分配后立即 claim，CommitExpert 成功释放，全部错误路径
经 release_miss_claim 释放。验证（efce31d8）：q4t_tests 106/106；
L2-8 5/5、L2-128 2/2 与 r2-baseline-s0 content 逐字一致
（compare-l2fix.py OVERALL: PASS）；bitexact-nu（cap=446+
hot-nu-15552+L2-8）1024/8192 BIT-EXACT。基线复用 r2-baseline-s0
仍有效（C=0 路径未受修复影响，跨二进制一致性经 L2-8 A/B 5/5
再确认）。

12:09 auto-r3c（pid 2621362，setsid）重跑链启动：
timing-collect-r3（efce31d8，L2-8，单条 45056，逐 miss 五段
计时——此前 timing-collect 跑在无 timing 特性的 r2 二进制上，
数据缺失）→ queue-r3-final（r3-cand-c256 → r3-cand-nu15552，
六档×3，基线复用 r2-baseline-s0）→ post-r3-affected（质量×3+
业务×3），日志 auto-r3c.log。预计 ~22:00–23:00 出两份 compare
报告。GEMM 冻结（第八次确认；git 核验 GEMM 文件最后改动
2026-09-27，本轮 diff 仅驻留层）。

下一步：跟踪 auto-r3c → 依据 r3 矩阵与逐 miss 计时数据定路径 C
（补载管线优化：r2 证据 25.7 miss/token × ~5.8 ms/miss ≈
150 ms/token 开销，需降至 ~2 ms/miss 量级才可能过 50% 门槛）。

## 五查门全过（7f013d73）；第三轮矩阵运行中（r3-cand-c256 → r3-cand-nu15552，六档×3，~20:00 出报告）（2026-10-01 09:59）

五查门全过（09:33–09:59）：q4t_tests 106/106；bitexact-c256
BIT-EXACT×2；bitexact-nu BIT-EXACT×2（按层非均匀 C 路径数值与
C=0 逐位一致）；跨二进制 C=0 逐位一致×2（基线复用 r2-baseline-s0
有效）；fault（500+注入消息 → 200+逐位一致）；cancel（取消后重发
与基线一致）。09:59:27 自动启动 queue-r3-final.sh（pid 2595010）：
r3-cand-c256（C=256+hot-256+L2-8）→ r3-cand-nu15552（cap=446+
hot-nu-15552+L2-8，用户授权超支），六档×3，预计 ~20:00 完成出两份
compare 报告；auto-r3b 随后自动 post-r3-affected（质量×2+业务×2）。
GEMM 冻结（第七次确认）。
下一步：跟踪矩阵 → 回填验收报告 §6 / 内存账（budget_backfill.py）/
R3 决策包定稿。

## 第三轮二进制构建修复（TimingStats 原子量破坏可移动性）；auto-r3-resume 链重启（2026-10-01 09:35）

auto-r3 链 09:20 在重建步骤失败停止（设计行为）：62edd6a 新增的
MoEResidency::TimingStats 含 std::atomic（非可移动），使
DecoderLayer 隐式移动构造被删除，model.cu `layers.resize` 编译失败
（此前 g++ -fsyntax-only 单文件语法检查未覆盖 libstdc++ 模板实例化）。
修复：TimingStats 装箱 unique_ptr（与 l2_mu_ 堆分配保可移动性模式
一致），21 处记录点改 `timing_->`，model.cu 保持原 resize；完整构建
零警告，二进制 7f013d73。GEMM 冻结（第七次确认，本轮仅驻留层
头文件/实现）。

执行链：auto-r3-resume.sh（09:33，setsid）重建（no-op）→ 五查门
verify-r3-binary.sh（在途 ~40 min：q4t_tests 106 + bitexact-c256 +
bitexact-nu + 跨二进制 C=0 逐位一致 + fault + cancel）→ 全过自动
启动 queue-r3-final.sh（r3-cand-c256 C=256+hot-256+L2-8 →
r3-cand-nu15552 cap=446+hot-nu-15552+L2-8，六档×3，基线复用
r2-baseline-s0）→ 两份 compare 报告；auto-r3b 观察器存活，矩阵后
自动 post-r3-affected（11 题质量×2 + 业务×2）。任一门失败即停。
timing-collect 已完成（第二轮二进制 45056 单条：loads=430524、
load_mb≈1.19 TB、misses=418236、L2 命中 0.16%）。预计 ~20:00
前后出 r3 两份对比。

## 第二轮矩阵完成：六档 decode 21.6–29.6% 基线（全 FAIL，如实记录）；post-r2-affected 完成（2026-10-01 09:12）

第二轮全矩阵（f2e9de7f，05:29–08:42）完成：基线 C=0 与候选
C=256+hot-256+L2-128 均六档×3 全过，逐位一致 6/6，目标档 token
合同 3/3（in=261887 out=257 finish=length，总上下文 262144）。
decode 比值 1024/4096/8192/45056/204800/261887 = 0.250/0.229/
0.216/0.254/0.296/0.229，全部 <50% 门槛（FAIL 如实记录，compare-
report-r2.txt）。L2-128 实测命中率 0.5–4.2%（261887 档
l2h=3040/l2m=2.89M≈0.1%），基本无效，与离线预测一致。内存峰值
（memory-peak.json 权威口径 rss+gpu）：基线 91.35 GB / 候选
77.74 GB（gpu 90.40/56.24 GB）；候选 TTFT 261887 档 1472.7 s
（基线 213.1 s，prefill 流式主导）。

结论不变：达标需同时 (a) 增 GPU 席位（nu-15552，已授权）与
(b) 控制补载开销（L2-128 弃用，第三轮降 L2-8 隔离 C 效应；
逐 miss 五段计时 timing-collect 将在第三轮重建前采集）。

执行链状态：post-r2-affected 在途（08:43 启动，11 题质量集
基线/候选 + 6 请求业务对照，预计 ~09:30）→ auto-r3 自动链
（timing-collect → 重建第三轮二进制 → 五查门 → r3 矩阵
r3-cand-c256 + r3-cand-nu15552，六档×3，基线复用 r2-baseline-s0）
→ auto-r3b 观察器（矩阵完成后自动跑 post-r3-affected：r3 二进制
上 11 题质量×2 配置 + 业务对照×2，最终候选的直接证据）。
GEMM 冻结（第六次确认，本轮零触碰）。

## 第三轮已获用户授权（按层 top-n nu-15552 + C=256 对照，超支接受）；auto-r3 将自动执行计时采集→重建→五查门→第三轮矩阵（2026-10-01 07:30）

用户决定（07:25，已记入
[MOE_RESIDENCY_R3_DECISION_2026-10-01.md](MOE_RESIDENCY_R3_DECISION_2026-10-01.md)
"用户决策"节）：(1) "即使内存预算超支，我支持你把C=256也做了，每层专家
按照命中次数的top-n来" → 第三轮执行 **nu-15552**（按层非均匀、每层专家
按冻结 business-set-v1 校准切分命中次数 top-n、DP 最优 C_l 256..446、
总 15552 槽、≈68.8 GB）+ **C=256 均匀同轮对照**；内存超支（超已接受
C=256 的 58.49 GB 约 9–10 GB）已明确接受。(2) "GEMM 暂时不修改,我们等
当前目标完成后再讨论" → GEMM 冻结（第六次确认；git 核验 GEMM 文件最后
改动 2026-09-27，本轮零触碰）。

执行链（auto-r3.sh 07:25 更新版，setsid 常驻 pid 2557253）：第二轮矩阵
结束 → timing-collect（第二轮二进制 f2e9de7f，C=256+hot-256+L2-128，
单条 45056，逐 miss 五段计时，路径 C 诊断数据）→ 重建第三轮二进制 →
五查门（q4t_tests 106 + bitexact-c256 均匀回归 + bitexact-nu 非均匀 +
跨二进制 C=0 逐位一致 + fault + cancel，全部通过才继续）→
queue-r3-final.sh：r3-cand-c256（C=256+hot-256+L2-8）→
r3-cand-nu15552（cap=446+L2-8），六档×3，基线复用 r2-baseline-s0（门
3b/5 验证跨二进制 C=0 逐位一致后有效）→ 两份 compare 报告。任一门失败
即停，不启动矩阵、不自动重试。

第二轮矩阵在途（f2e9de7f，07:15 时点）：基线 C=0 六档×3 全过（decode
16.8–18.7 tps，261887 目标档 ttft≈213 s/decode≈16.8 tps/逐位一致/总
上下文 262144）；候选 C=256+hot-256+L2-128 已过 1024/4096/8192/45056，
204800 在途（06:23 起，每请求 ~20 min），261887 待跑，预计 09:00–09:30
全矩阵完成，随后 post-r2-affected（11 题质量集+6 请求业务对照）。在途
证据不变：L2-128 命中率极低（4096~0.7%、45056~0.17%）基本无效且新增
~19 GB 超支；第一轮 decode 22.0–29.8% 基线全 FAIL，第三轮 nu-15552
离线 sim miss 10701（较均匀 C=324 少 16.5%）为同内存最优。

## 第二轮候选在途（204800 1/3）；补载管线分析+逐 miss 计时采集就绪，A/C 决策待授权（2026-10-01 07:00）

第二轮矩阵在途（f2e9de7f）：基线 C=0 六档全过（decode 16.8–18.7 tps，
261887 目标档 ttft≈213 s/decode≈16.8 tps/逐位一致/总上下文 262144）；
候选 C=256+hot-256+L2-128 已过 1024/4096/8192/45056，204800 在途
（1/3，每请求 ~20 min），261887 待跑，预计 09:00–09:30 全矩阵完成。
实测 decode 比值 1024=24.7%/4096=22.8%/8192=21.5%/45056=25.9%（全
<50% FAIL）。关键证据：L2-128 命中率极低（4096~0.7%、45056~0.17%、
204800 l2h≈2339/l2m≈2.23M~0.1%）基本无效，且新增 ~19 GB 超支（候选
实测峰值 rss+gpu≈77.7 GB vs 已接受 C=256 58.49 GB；swap 1.03 GB 单列）。

补载管线（路径 C）静态分析完成（纯代码，GEMM 冻结未动）：decode 热路径
每层 D2H+`cudaStreamSynchronize`（moe.cu:418–434，基线/候选共有，非
减速之源）；单 miss 有效 6.2 ms = NVMe ~1.44–2 ms + ~4 ms 管线开销
（stage/commit 串行无重叠 + 逐专家串行 H2D + 互斥/条件变量往返，见
06:38/07:00 日志）。逐 miss 五段计时（Q4T_RESIDENCY_TIMING=1）代码已
就位但此前未开启；timing-collect.sh 已备好（候选同配置单条 45056，
~5 min），待第二轮释放 GPU 后立即运行（两服务并存 ~155 GB > 122 GB
RAM，不可并行）。R3 决策包补「补载管线静态分析」节。

**待用户授权**：路径 C（推荐，弃 L2-128、保持 C=256=58.49 GB、优化补载
管线，定量 decode≈54% 基线过门槛）/ 路径 A（增 C：nu-15552 推荐
≈68.8 GB +10.3 GB / C=324 / C=384）/ A+C 并行。第二轮实测落地后回填
决策包定稿。GEMM 冻结（第五次确认）。

## 第二轮基线完成、候选在途；第三轮决策包成文，GEMM 冻结第五次确认（2026-10-01 06:20）

第二轮矩阵在途（f2e9de7f，05:29 启动）：基线 C=0 六档×3 全过（05:29–05:55，
26 min；261887 档 ttft≈213 s、decode≈16.9 tps、逐位一致、总上下文 262144）；
候选 C=256+hot-256+L2-128 已过 1024/4096/8192、45056 档在途，预计 12:00 前后
全矩阵完成。在途关键证据：**L2-128 命中率极低**（4096 档 ~0.7%、45056 档
~0.17%），对该工作集基本无效，与离线预测一致；decode 瓶颈定量（45056 档反推）：
基线 decode≈15.1 s，候选 dmiss≈6500×~6.2 ms 串行≈40 s → decode_tps≈28% 基线，
与第一轮 22–30% 吻合；**达标需把 decode miss 降 ~2.7×**。

第三轮决策包成文：
[MOE_RESIDENCY_R3_DECISION_2026-10-01.md](MOE_RESIDENCY_R3_DECISION_2026-10-01.md)。
推荐 **nu-15552**（按层非均匀、每层专家按校准集命中次数 top-n、DP 最优
C_l 256..446、总 15552 槽、≈68.8 GB、同内存比 C=324 均匀少 16.5% miss），与
用户"每层专家按命中次数 top-n"口径一致；备选 C=324 均匀（≈68.8 GB）/
C=384（≈76.7 GB）；不新增内存备选：decode 导向热点表（C=256 保持 58.49 GB）
或补载管线并行优化（GEMM 仍冻结）。第三轮属新增内存超支（+10.3 GB 相对已接受
C=256），auto-r3.sh 只到五查门+决策摘要、不自动启动矩阵，**待用户明确授权**。
GEMM 冻结第五次确认；工作树 diff 仅驻留层/服务层/测试工具与文档，无 GEMM 改动。
261887/261888 边界不再追究（目标档=总上下文 262144=输入 261887+输出 257）。
下一步：跟踪第二轮矩阵 → compare-report-r2.txt 实测列回填决策包 → 向用户提交
第三轮授权决策。

## 第二轮矩阵运行中；第三轮（按层非均匀 C）准备完毕，待超支决策（2026-10-01 05:55）

门 #4（f2e9de7f）05:29 四查全过（q4t_tests 106/106、bitexact-c256
BIT-EXACT×2、affected-fault、affected-cancel）；auto-r2c 05:29 自动启动
第二轮矩阵（L2-128+C=256+常驻池，六档×3，r2-* 标签）：基线 C=0 已过
五档、正在 261887 档，随后候选档。第一轮实测 decode 22.0–29.8% 基线
（全 <50% 门槛 FAIL，根因逐 miss 逐层串行 NVMe 补载）；第二轮 L2-128
离线预测 ~30%，预计仍低于门槛，达标需第三轮。

第三轮准备完毕（代码未验证；build/q4t 在第二轮全程保持 f2e9de7f，
矩阵不受影响）：(1) model.cu 按层非均匀 C——c_layer[l]=min(热点表长,
--moe-resident-slots 上限)，均匀表保持旧行为；运行时路径（MoEResidency
每层 Init/Slots/Layout、moe.cu prefill 分组与 C<topk 守卫、decode 核
per-expert 步长）结构上按层安全，已逐项复核；(2) chat_server.cpp 内存
预算估算改按层实际驻留数（旧式按均匀 C，nu-15552 cap=446 时会把权重
少计 ~16.2 GB），槽字节改用 MoEResidencyLayerBytes 精确值；(3)
tools/trace/make_hot_list_nu.py + hot-nu-15552.json（DP 最优按层 C_l
256..446、总 15552 槽，sim miss 10701 vs 均匀 C=324 的 12818，-16.5%）
+ hot-324/384.json + queue-r3.sh/queue-r3-nu.sh + bitexact-nu.sh +
verify-r3-binary.sh（五查门：q4t_tests + bitexact-c256 均匀回归 +
bitexact-nu 非均匀 + fault + cancel）。auto-r3.sh（setsid）等 auto-r2c
结束 → 重建 → 五查门 → 决策摘要；不自动启动第三轮矩阵。内存估算
（槽 2,764,816 B，第一轮 C=256 实测 58.49 GB 为基准）：C=324/nu-15552
+L2-8 ≈ 68.8 GB、C=384+L2-8 ≈ 76.7 GB——均超出用户已接受的 C=256
超支（58.49 GB），启动第三轮需用户明确授权。GEMM 冻结（第四次确认）。

## 四查门 #4 运行中（新二进制 f2e9de7f）；fault 钩子全局一次性修复（2026-10-01 05:10）

四查门 #3（04:49，二进制 b22a1049）：q4t_tests 106/106 PASS、
bitexact 两档 BIT-EXACT=True PASS、affected-cancel PASS（取消后重发
逐位一致）；affected-fault FAIL 且暴露两个缺陷：(1) "first" 钩子按层
布防——每层实例各自 arm，step1 在 layer 0 首个 stage 失败后，step2
又在 layer 1 首个 stage 失败（server.log diag 证实：step1 死于
layer 0、step2 死于 layer 1），恢复合同（step2 必须成功）被破坏；
(2) 错误消息在 LoadPhase1 被压成 "residency parallel stage failed"、
在 ChunkPrefillReq（仅 bool ok）再次丢失，HTTP 500 正文不含
"fault injection"，测试无法验证失败原因。

修复（仅驻留层+服务层，GEMM 冻结未动；二进制 f2e9de7f，05:05
零警告构建）：(1) "first" 钩子改全局一次性（g_first_fault_fired
原子量，全模型恰好一个请求期 stage 失败；expert-id 模式语义不变）；
(2) 错误消息全链传播：LoadPlan.first_err_msg（首个失败 worker 写入，
cv 等待建立 happens-before）→ LoadPhase1 "residency parallel stage
failed: <msg>" → ChunkPrefillReq.err → HTTP 500 正文
"scheduled chunk prefill failed: residency fault injection: expert N
(test hook)"。门 #3 日志封存 verify-r2-binary.log-run3-b22a1049。
05:07 启动门 #4（f2e9de7f，setsid）；auto-r2c.sh 观察门 #4，四查全过
才 setsid 启动第二轮矩阵（queue-r2-l2pool.sh，L2-128+C=256+常驻池，
六档×3，r2-* tag）→ 再跑 post-r2-affected.sh（11 题质量集+6 请求
业务对照）。NVMe 单 miss 分解（05:05 实测）：3.28 MB 随机 pread
未命中页缓存中位 1.44 ms/p90 2.07 ms，顺序 4 MB 2.3 GB/s——NVMe 非
6.2 ms 有效单 miss 成本之源，~4 ms 管线开销（页缓存压力/单流同步/
chunk 互斥往返）待第三轮逐 miss 计时定位，详见
MOE_RESIDENCY_L2_PREDICTION_2026-10-01.md 新增节。

## 第一轮矩阵完成：六档 decode 22.0–29.8% 基线（全 FAIL）；四查门修复后重跑中（2026-10-01 04:50）

第一轮冻结全矩阵（23:46 二进制 4c5cddea，00:13–03:37）完成：基线 C=0
与候选 C=256+hot-256 均六档×3 全过，逐位一致 6/6（18/18 输出），目标档
token 合同 3/3（in=261887 out=257 finish=length，总上下文 262144）。
decode 比值 1024/4096/8192/45056/204800/261887 = 0.246/0.233/0.220/
0.264/0.298/0.229，全部 <50% 门槛（FAIL 如实记录）。内存峰值：基线
91.33 GB / 候选 58.49 GB（用户已接受超支）。候选加载量 261887 档
2891k/7.99 TB×3。根因不变：逐 miss 逐层串行 NVMe 补载
（evictions==loads，非热点槽池常满）。

四查门（verify-r2-binary.sh）第一轮跑（04:18，旧二进制 104c8b09）：
q4t_tests 106/106 PASS、bitexact-c256 两档 BIT-EXACT=True PASS、
affected-cancel PASS（mid-prefill/mid-decode 取消后同请求重发逐位一致，
槽位/状态恢复正确）；affected-fault FAIL——钩子按专家 id=401 布防而
401 是 layer 2 热点，InitHot 并行装载阶段触发钩子杀死启动
（"residency parallel stage failed"），curl rc=7。

修复（仅驻留层，GEMM 冻结未动；新二进制 b22a1049，04:43 构建）：
(1) 故障钩子新增 "first" 模式（Q4T_RESIDENCY_FAIL_EXPERT=first：首个
请求期 stage 失败）+ init_done_ 守卫（InitHot 结束前钩子不触发，布防
热点专家不再杀死启动）；affected-fault.sh 改用 first 模式。
(2) L2 recency 时钟与槽位 LRU tick 分离（l2_recency_）：L2 staging
不再推进槽位 tick，同调用内槽位 commit 不会压过 in-call 命中破坏
LRU victim 顺序。
四查门重跑（04:49 启动，setsid，新二进制，日志 verify-r2-binary.log）：
q4t_tests + bitexact + affected-fault + affected-cancel 全过才自动进入
第二轮矩阵。

L2 离线预测（MOE_RESIDENCY_L2_PREDICTION_2026-10-01.md，聊天轨迹
校准→留出 + 联合 C×L2 扫描）：第二轮 L2-128+C=256 预计 ~30% 基线
（L2-128 只接 ~6% GPU miss，仍低于 50% 门槛）；同内存下加 GPU 槽优于
加 CPU L2（C=384 无 L2，78.6 GB，~61% > C=256+L2-128，78.6 GB，~30%）；
验收夹具工作集 W≈270（第一轮 miss 率反推）明显小于聊天轨迹 349，
**C=324（68.4 GB，+14.4 GB 超支）在夹具上可能接近基线**——第三轮
首选候选。内存超支幅度（C=324/384）属新增超支，需用户明确授权后
才启动第三轮；NVMe 单 miss 6.2ms（≈450 MB/s）偏低，第三轮可并行做
逐 miss 时延分解/预取（GEMM 仍冻结）。GEMM 冻结用户再次确认
（"GEMM 暂时不修改,我们等当前目标完成后再讨论"）。

## 第二轮自动推进编排已就位；GEMM 冻结第三次确认（2026-10-01 02:48）

用户 2026-10-01 02:4x 第三次确认 **GEMM 冻结至本目标完成**（"GEMM 暂时不
修改,我们等当前目标完成后再讨论"）；第二轮范围（L2/钩子/常驻线程池）本就
不含 GEMM，无需调整。候选矩阵 261887 档 1/3（02:41），预计 03:45–04:00
完成。已部署 auto-r2.sh（setsid 常驻，日志 auto-r2.log）：等第一轮队列
结束 → verify-r2-binary.sh（q4t_tests 106 + bitexact-c256 +
affected-fault + affected-cancel）→ **严格四查门**（任一失败即停、不启动
第二轮矩阵、不自动重试）→ 通过才 setsid 启动 queue-r2-l2pool.sh（第二轮
全矩阵）→ 记录 r2 终态。verify-r2-binary.sh 补 pipefail 修复 rc 透传
（仅测试工具）。第二轮内存口径维持冻结计划：L2=128/层（+20.13 GB
pinned，启动即全额），预计峰值 ≈78.6 GB——超出用户已接受的 57.71 GB，
属新增超支，实测值将如实进入验收报告并单列决策（降 L2 选项：64/层
+10.07 GB、32/层 +5.03 GB，Q4T_MOE_L2_SLOTS 无需改码即可复测）。

## 262144 完整内存账已建立；候选矩阵 5/6 档完成（2026-10-01 02:30）

目标"先建立可复核完整内存账"完成：
[MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md](MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md)
（预算侧逐项字节数+代码来源 + 实测侧 rss+gpu 权威口径 + 核对差异）。
关键数字：基线 C=0 实测峰值 91.33 GB（gpu 90.40）；候选 C=256 第一轮
58.49 GB（gpu 56.24，矩阵进行中已稳定）；第二轮 L2-128 池 = 48×128×
3,276,816 B = 20.13 GB pinned，预计峰值 ≈78.6 GB——超出用户已接受的
57.71 GB 量级，属新增超支，账内决策记录已标注（可降 L2：64/层
+10.07 GB、32/层 +5.03 GB）。swap 峰值 1.4 GB 单列，不掩盖超支。
候选矩阵 5/6 档完成（1024/4096/8192/45056/204800 各 3/3 逐位一致），
decode 22–29% 基线（<50% 门槛，根因逐步串行 NVMe 补载已定位）；
261887 档 02:13 启动，预计 03:30–03:45 完成。下一步：compare_e2e.py →
新二进制 GPU 验证（q4t_tests 106 + bitexact-c256 + affected 四查）→
第二轮全矩阵（queue-r2-l2pool.sh）。

## 分层专家驻留：第二轮冻结（L2+常驻线程池），矩阵 03:45–04:00 完成

用户 2026-10-01 再次确认 **GEMM 冻结至本目标完成**（moe_gemm.cu /
moe_decode.cu 等一律不动；驻留层/服务层/测试可改）。23:46 二进制候选
矩阵四档已确认 decode 22–27% 基线（<50% 门槛），根因是逐步、逐层串行
NVMe 补载；204800 档第 3 请求 prefill 中，随后 261887 档，预计
03:45–04:00 完成。矩阵结束后：compare_e2e.py 冻结口径对比 → 新二进制
（L2+钩子+常驻线程池，commit 35b9f7d 起）跑 q4t_tests（106 项）+
bitexact-c256 + affected 四查 → 第二轮全矩阵（同六档×3、同夹具、
--request-deadline-ms 10800000）。第二轮范围冻结见计划文档
"第二轮（2026-10-01）"节：L2 缓存（128/层，默认）+ 补载故障钩子 +
LoadPhase1 常驻线程池（消除每次补载块的 8 线程创建/回收，实测
0.173 ms/轮，decode 每步约 4 ms）；GEMM 未动。若第二轮仍有档 <50%，
第三轮候选：逐 miss 时延分解（NVMe pread 实测）、L2 容量调参、
C=324（内存约 66 GB，需用户再次确认超支）。CPU 微基准（无 NVMe/GPU，
矩阵运行中安全执行）：swizzle 双块 0.210 ms/专家（非瓶颈）、8 线程
创建+join 0.173 ms/轮。磁盘余 2.4 GB，持续观察。

## 分层专家驻留：L2 CPU 缓存与补载故障钩子已实现（2026-10-01 凌晨，矩阵运行中）

候选矩阵（23:46 二进制，4c5cddea）仍在跑（204800 档），GPU 验证待其
结束。本轮完成（仅驻留层/服务层/测试，GEMM 冻结未动）：
(1) L2 CPU 专家缓存——每层 pinned LRU 池（Q4T_MOE_L2_SLOTS 默认 128，
约 +17 GB 固定项，预算核算后 max_len=262144 不被 cap），命中快路径
免 NVMe 读，victim 跳过 in-flight/claimed，全忙同步等待；统计新增
l2h/l2m/l2ev/nvme_mb。这是冻结出口“CPU 二级缓存”的实现，供候选
decode 若 <50% 门槛时启用。
(2) 一次性补载故障钩子 Q4T_RESIDENCY_FAIL_EXPERT（仅测试默认关），
affected-fault.sh 两步合同所需。
(3) 新单测 residency_l2_cache_hit_evict。零警告构建；23:46 冻结二进制
封存于 bin-2346-fixed/；build/q4t 现为 L2+钩子构建（未 GPU 验证，
不得用于验收）。矩阵完成后：compare_e2e.py → 新二进制 q4t_tests
（106 项）+ bitexact-c256 + affected 四查 → 填验收报告。

## 分层专家驻留：修复二进制全矩阵重跑中（2026-10-01 00:13 启动）

23:46 二进制（sha256 4c5cddea，commit 509665d，工作区干净）按冻结轮
重跑全矩阵：基线 C=0 → 候选 C=256+hot-256，两配置统一
--request-deadline-ms 10800000（与冻结客户端超时 7200s/10800s 对齐），
六档×3，内存监控。runner 新增 --request-deadline-ms 透传
（tools/evalscope/run_acceptance.py，仅测试工具，无运行时改动）。
用户 2026-10-01 再次确认：目标档口径为**总上下文 262144**（非输入
256k），261887/261888 一字之差不再追究；C=256 每层按校准集命中次数
top-n，内存预算超支（实测 57.71 GB vs 54 GB）明确接受；GEMM 冻结至
本目标完成。旧 18:06 构建基线封存为 e2e-baseline-s0-1806-build，20:24
二进制候选部分结果封存为 e2e-cand-c256-2024-binary-partial，均保留。
首次启动（00:08）因进程组随执行会话回收而整体终止（服务端模型加载
中途消失，无 OOM/崩溃记录），改 setsid 完全分离后 00:13 重启成功。
磁盘余量紧张（2.9 GB），矩阵产物预计 <1 GB，持续观察。下一步：矩阵
完成后冻结口径对比（compare_e2e.py）→ 填验收报告；任一档 decode <50%
则按冻结出口评估 CPU 二级缓存或更大 C/预取，不放宽门槛。

## 分层专家驻留：decode 慢速 GEMM 回退与 20 分钟超时已修复，全矩阵重跑中（2026-09-30 深夜）

冻结轮首跑（20:24 二进制）：基线 C=0 六档×3 全过（目标档 3/3
in=261887 out=257，内存峰值 91.35 GB）；候选 C=256+hot-256 三档逐位
一致但 decode 仅基线 22%（50% 门槛 FAIL），45056 档被服务端 20 分钟
默认 deadline 取消后 runner 中止。根因：(a) 槽位模式 E==C≠512 使
decode 落到 host 编排分组 GEMM（约 4x 慢），与补载无关；(b) 服务端
默认 deadline 短于冻结客户端超时（7200s/10800s）。修复（仅驻留层/
服务层，GEMM 冻结未动）：槽位模式 decode 直调 MoEDeviceDecode（核按
常量 per-expert 步长寻址，E 无关；workspace 与 GEMM 路径逐项一致）；
新增 --request-deadline-ms（上限 10800000）；驻留统计分列
decode/prefill lookups/misses。验证：零警告构建（23:38 二进制）；
q4t_tests 105/105；bitexact-c256 1024/8192 均 BIT-EXACT=True；C=256
启动 3/3。ab-decode 中 20:24 二进制的 c256/c512full 启动段错误未复现
（当前二进制 3/3 OK），记为瞬态，矩阵中持续观察。目标长度档按用户
口径为**总上下文 262144**（输入 261887 + 输出 257），边界调查已关
闭。内存峰值（RSS+GPU 口径）：s0=91.33 GB，c256=57.71 GB——超 54 GB
门槛 3.7 GB，用户已明确接受（决策记录见计划文档）。下一步：同一
23:38 二进制重跑全矩阵（基线 C=0 + 候选 C=256+hot-256，均带
--request-deadline-ms 10800000，六档×3）→ 冻结口径对比。
验收报告骨架：
[MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md](MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md)；
计划与冻结轮：[MOE_RESIDENCY_PLAN_2026-09-30.md](MOE_RESIDENCY_PLAN_2026-09-30.md)。

## 每层驻留名单（90%选择覆盖）已升级为校准→留出口径

2026-09-29：同样本事后口径升级为校准→留出：16唯一请求按(领域,长度)
分层8/8（请求7无decode，decode为8/7）；每(阶段,层)按校准频次排名、
留出定最小N。decode N90逐层144–324（和10387槽），prefill 136–287
（和10806）；两阶段共用统一容量取324/层=15552槽≈43.0 GB（slot
2,765,056 B，仅路由专家；共享专家/KV/状态/workspace另计）。留出N90
系统性高于同样本5–25%，部署容量应以留出口径为准，同样本仅作乐观
下界。逐层具体专家ID名单在
.q4t-work/moe-topn-90-20260929/expert_lists_90.json，作为下一步驻留
设计的静态基线候选；进入驻留设计前须以独立业务材料重校准、明确总
预算扣除项与补载安全合同。容量画像，非运行时收益证明；无模型/HTTP
运行。复现：tools/trace/layer_topn_heldout.py（2026-09-30入库，与封存
结果逐字节一致）。

## 主试验原始路由独立局部性分析已收敛（v2口径）

2026-09-29：v1把48层bitmask跨层OR造成专家ID碰撞，跨层工作集结论
作废；v2改逐层计算、跨层求和实例数（总空间48×512=24576）。单token
工作集48×10=480实例；逐层8/16/32-token窗口工作集45.2/70.4/104.5
(/512)，增长缓慢无饱和。时间局部性集中在≤8 token（lag-1重合为随机
基线2.3倍，lag-32回落到基线以下）；层间Top-32 Jaccard 0.040仅略高于
随机，跨层共享无结构红利；prefill/decode同层Top-64 Jaccard 0.116，
热点不可跨阶段复用。方向与回放/影子结论一致：收益空间在逐层小容量
LRU（32/64槽/层）。主试验原始路由已导出为可读文本（143M，gzip后43M，
27条目）供外部分析；导出与分析脚本均已入库。无模型/HTTP运行。报告
.q4t-work/moe-locality-20260929/REPORT.md（v2），证据
.q4t-work/moe-raw-export-20260929/。

## MoE分析工具收口（2026-09-30）

09-29分析脚本layer_topn_heldout.py与analyze_locality.py入库
tools/trace/（与封存结果逐项一致）；test_distribution.py（8项）与
test_hybrid.py（6项）接入公共host CTest，6项测试全部通过；三份MoE
专题文档（分层Top-N、混合分析、EvalScope多场景）补登docs/README.md
索引。公共host合同现为6项；distribution合同需要numpy（CI经apt安装
python3-numpy，本机优先.q4t-work/venvs/public-host专用venv）。无
运行时/模型/reference改动。

**默认入口已更新为`text-v1-moe-trace-20260928` / `3b414633`。**
用户已明确接受本轮MoE采集候选的实测性能代价。单Thor、文本greedy、
MTP/媒体关、max_seq=1；采集仍默认关闭，按需显式启用。
直接安装已验收二进制原件，2252个封存文件、运行时源码、发布包和构建缓存
身份复核通过，未重建默认静态库。安装后的1024-token精确输出/usage/SSE、
单槽健康与回收、正常停止通过；实际恢复旧98a75fb4后重新安装新版。
当前没有常驻服务。原质量/五档/失败专项按相同二进制身份复用，未重复矩阵。

包：.q4t-work/releases/text-v1-moe-trace-20260928/；默认身份见
build/q4t.release.json。部署证据与rollback-default.py位于
.q4t-work/moe-trace-deployment-20260928/；停止服务后可恢复旧默认。
成本接受：.q4t-work/moe-trace-acceptance-20260928/decision.json。
原测量、参考、候选封存时的待接受记录与旧发布包保留；本次接受决定
不改写严格持平未通过的事实，也不放宽以后通用门槛。本阶段关闭。

## 分层Top-N目标曲线已生成

48聊天输入、逐层分阶段完整N=1..512曲线；沿频率排名求达到目标的最小N，
不是最佳专家组合或运行时最优缓存。prefill达90%选择覆盖需171–321专家，
达80%整组概率需305–423；decode对应160–342、226–422。层0/17/47 prefill
90%选择目标分别312/321/171，层间差异明确。全曲线边界/单调/最小性和
与旧三档1,152项独立成员集合指标核对通过，无新推理。
完整48层表见[分层Top-N报告](MOE_LAYER_TOPN_2026-09-28.md)。

## 同口径Top-256集合分布已补齐

相同48聊天输入，prefill/decode选择覆盖87.29%/92.37%，每次top10平均落入
8.729/9.237个；全部十个落入概率43.50%/58.45%。源摘要、频次、概率界限及
Top128到Top256单调性核对通过，无新推理。这是分层同样本集合分布，非
LRU缓存命中或全部层同时覆盖；详见[分布报告](MOE_DISTRIBUTION_2026-09-28.md)。

## 同口径Top-128集合分布已补齐

相同48聊天输入，prefill/decode选择覆盖67.60%/71.42%，每次top10平均落入
6.760/7.142个；全部十个落入概率14.76%/13.69%。原始路由精确计数与频次、
源摘要及Top30集合单调性核对通过，无新模型执行；不是缓存泛化评估。
完整逐层表与专家概率见.q4t-work/moe-top128-20260928/及[分布报告](MOE_DISTRIBUTION_2026-09-28.md)。

## 每层Top-30集合分布已导出

48聊天输入、同样本分阶段逐层选Top30；prefill/decode选择覆盖33.41%/30.76%，
每次实际top10平均落入3.341/3.076个，全部十个都落入的概率0.653%/0.0974%。
各层专家单独概率和0–10个重合分布已保存；从原始token路由精确计数并匹配
频次向量，无独立性假设。属于描述性统计，不是新请求缓存评估，无新模型
运行。入口top_expert_sets.py，详见[分布报告](MOE_DISTRIBUTION_2026-09-28.md)。

## 专家进入top-10的逐层出现概率已导出

按用户明确口径，以token路由次数为分母，导出48聊天输入的prefill/decode
全部512专家概率及每层最高10位。概率等于原单专家选择份额的10倍，各层
概率和为10；不是联合概率或缓存命中。层47 prefill专家157为92.42%，
decode最常见专家442为23.90%。全部层/专家与源摘要核对通过，无新推理。
入口expert_probabilities.py，详见[分布报告补充](MOE_DISTRIBUTION_2026-09-28.md)。

## MoE专家ID分布统计完成（与缓存命中分开）

当前同二进制/模型的9批139请求核验，59次同输入重复的完整路由一致；分组
去重后80输入，其中48聊天材料为主分析。已提交ID共1,568,887,680个。
prefill/decode每层平均活跃511.48/473.23专家，Top128占比67.60%/71.42%；
两阶段同层Top64名单仅重合18.59%。decode同场景请求名单重合76.04%，
跨场景35.08%，模板/长度混杂使其不能被解释为语义专用性。逐层、逐专家、
块/请求出现率、等权/路由权重、前后半段及场景/长度分组均保存。
384个历史阶段/层完整向量交叉核对一致，全部分组另行重算；8项合同首轮
7通过，失败forward计数边界修复后单独重验通过。无新HTTP、缓存模拟、
吞吐或常驻名单结论。下一步用独立业务材料验证分布稳定性。
详见[完整分布报告](MOE_DISTRIBUTION_2026-09-28.md)，证据.q4t-work/moe-distribution-20260928/。

## 固定热点+LRU初步分析完成

复用24条轨迹，64/128/256槽比较固定0/25/50/75/100%，同预算、旧冻结排名，
无未来训练。跨请求保留时所有非零固定比例的汇总总加载均比纯LRU更多；
每请求冷重置时256槽固定192+动态64的总量少约9.2%，18/24改善、最短六条
更差。固定填充已计费；仅本批探索性候选，不认定统一最优比例。6项直接
合同、4,432,320次组/策略检查及288条端点匹配通过，无新HTTP或运行时变更。
下一步按生命周期区分策略，并以独立业务留出请求验证；见[混合分析](MOE_HYBRID_2026-09-28.md)。

## 同轨迹128/256槽容量扩展完成

复用24条EvalScope轨迹离线补算四档容量，无新HTTP；2,363,904项需求组/策略
独立核对通过，32/64逐请求统计与原结果完全一致。128/256槽连续LRU选择命中
83.73%/94.57%、整组33.11%/66.61%，预算17.016/34.005GB，仅路由专家及staging。
256槽每请求重置时，最长六请求总逻辑加载高于静态：冷prefill开销抵消decode
改善。因此将128/256纳入离线容量权衡，需结合冷启动与跨请求保留；没有
实际加载/驱逐或吞吐证明。原质量/成本边界不变。见[容量补充](MOE_EVALSCOPE_SCENARIOS_2026-09-28.md)。

## EvalScope新增多场景采样完成

按用户要求新增六类任务各4条，共24条EvalScope单流请求，实际输入114–13,607
token、输出均128截断。24/24 HTTP正常，18,432项逐层独立重算一致；保存
逐请求路由/JSON、原始数据库及场景CSV。32/64槽、两模式均24/24减少decode
缺失和总逻辑加载；64槽连续模式静态/LRU选择命中32.48%/69.18%，LRU整组
全命中13.21%。保留64槽LRU候选，尚无真实补载收益证明。合成相关样本、
截断输出不代表生产分布或质量通过；旧性能不持平结论不变，未重复五档。
运行时/默认部署未变，服务已停止。详见[多场景报告](MOE_EVALSCOPE_SCENARIOS_2026-09-28.md)。

## MoE 请求完成边界影子观察已交付，严格持平未通过

用户确认减少专家常驻内存、尽量保持性能的方向后推进；按最新约定先收敛
工具/采样/分析，再补最终验收，已通过且身份未变的证据复用，不反复测试。
独立旁路消费原子发布的完整请求，32/64槽静态/LRU、prefill_reset/continuous，
统计全部已提交decode；不提供逐层及时可见性，不控制权重。运行时及默认
部署仍为3b414633，124项源码/二进制/检查器与已接受包一致。

13项影子合同、既有13项回放合同通过，无SKIP；质量11/11，两个完整五档
各15请求均匹配冻结输出，新材料8条HTTP/SSE正常。34份影子轨迹26112项
逐层统计与独立重算一致，41条evalscope原始记录及8份SSE核对通过。
新8请求（5个对话）两容量/两模式均减少decode缺失和总逻辑加载；64槽
连续模式选择命中70.65%、整组全命中15.52%，不换算吞吐或实际内存节省。
静态新材料迁移弱、固定顺序、21K而非200K长文及128输出上限限制外推。

最终开启影子相对同二进制采集on/影子off，8K decode均值−0.244%，后两次
落在对照范围较慢一侧，严格持平未通过；不追加采样，不自动接受新增成本。
观察器自身RSS峰值约24.2MiB，积压峰值1，最多15.28秒才完成请求后统计；
不证明补载及时。仅按需诊断交付，默认不开启；真实加载/驱逐和预取均未做。
范围、成本、复现与边界见[影子报告](MOE_SHADOW_2026-09-28.md)，
证据.q4t-work/moe-shadow-20260928/。后续保留64槽LRU候选，先明确实际容量
及补载/复用安全合同，不自动进入真实驻留管理。所有本轮服务已停止。

## MoE 固定容量离线回放已交付

静态热点与整组LRU、每层32/64/128/256槽、三种重置/保留模式完成。
8条校准、8条留出、5条既有独立五档输入；源117文件与原封存匹配。
13项缓存合同、15项来源检查、3项CLI拒绝通过；4992组冷decode独立重算
一致。工具与合同接入host，默认运行时/二进制/模型/reference未改，无新HTTP。
32/64槽LRU在13条请求、三模式的decode均减少缺失；但长prefill在逐请求
重置时可抵消改善，128/256槽不稳定优于静态。建议仅32/64槽进入有界在线
影子统计，优先64；当时限定离线，后续请求完成边界影子结果见上文。预算含scale、对齐与10槽staging，
不把覆盖或逻辑加载转换为吞吐。合同、限制、逐请求分布与复现见
[回放报告](MOE_CACHE_REPLAY_2026-09-28.md)，证据在
.q4t-work/moe-cache-replay-20260928/。未来真实驻留仍需容量实测与独立验收。

## MoE Router 首轮真实采集观察

2026-09-28在默认3b414633上显式启用采集：主试验四类材料、两个种子、
约1K/8K，16条唯一聊天请求+4条精确重复；每条输出128 token，统一分析
前64次decode。20份轨迹完整，共38695680个专家ID，4/4重复文本和路由
完全一致。裸prompt试采另20条保留，不与聊天、关闭思考的主试验混算。
每层512专家中固定训练top128，对8条留出覆盖82.1%；对同二进制既有五档
独立输入覆盖70.4%–76.7%。同请求prompt top128对留出decode仅覆盖39.6%。
相邻top-10集合重合38.5%，同窗口随机不同token对为21.0%；存在时间局部性，
但各层9.4%–59.9%差异很大。全部覆盖指专家选择次数，不是权重贡献、cache
命中率或SSD收益。共享模板、每类仅两个种子，不能把任务/语言差异解释为
语义专用专家，也不能把长度/内容共同变化当因果。独立重算与分层图核对通过。
证据和复现脚本：.q4t-work/moe-router-study-20260928/findings.md。
后续固定容量回放现已完成，结果见上文；此首轮观察未实现缓存或offload，
未运行新性能验收。测试服务已停止，
默认二进制、部署元数据与既有参考均未改；采集仍按需显式开启。

## MoE 受控采集已交付，实测代价已接受

实现2531988/main，默认版本3b414633；本机发布包：
.q4t-work/releases/text-v1-moe-trace-20260928/（q4t与检查器、源码和摘要）。
在仓库根目录按[轨迹工具](../tools/trace/README.md)显式启用；默认关闭，
仅单序列文本greedy、MTP/媒体关。已覆盖scheduler、chunked prefill及
inline/fallback。阶段、位置、层号来自实际入口与ModelSequence，未改Router
算法或同步策略。8192 chunk固定15 MiB device + 60 MiB pinned；后台编码
写盘，队列满/配额/IO失败停收并保持incomplete。OOM禁用采集并清理部分
池；其他CUDA错误保留服务失败语义，最终drain失败隔离缓冲至进程退出。

保存实际输入token、HTTP ID、请求终态、逐层专家ID，以及二进制、模型
index/config、命令/相关环境和工作负载摘要。离线工具校验来源与完整性，
输出prefill/decode频次和同样本事后覆盖；不推导缓存收益或吞吐。
普通文件IO仍可能阻塞关闭，未提供故障文件系统硬超时或长期采样保证。

Thor零警告构建、46项host通过；公共CI的42项host、49项格式、16项框架
反例、4项证据审计和15项来源分析检查通过，无SKIP。
[代码CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/36385196276)。
最终候选off/on质量各11/11，旧默认/off/on各完成五档、每档3次、输出256
Token，全部匹配冻结参考。26份完整轨迹共519929760个专家ID，96个层/阶段
统计组。67条evalscope原始记录及9条直接生命周期SSE审计通过。
取消8组、decode失败4组、生命周期5组、prefill读回失败2组通过。
采集专项15项：12项在4bc183a3通过，按未变producer/worker/同步与调度
代码身份复用；最终候选补跑device/pinned OOM与manifest close失败3项。
55项输入/63项模板证据按未变实现复用。106个设备函数与上一候选一致；
相对旧默认98a75fb4仅既有GDN变体，精确cubin对复用60例有限数值证据，不宣称整模型
数值oracle或设备代码全部与默认相同。初版与修正前证据保留，不混用身份。

下表默认指测量时旧版98a75fb4；decode为调和均值，TTFT为算术均值。
原始三次及首请求/后续范围另存。

| 上下文 | off相对默认 decode | off相对默认 TTFT | on相对off decode | on相对off TTFT |
|---|---:|---:|---:|---:|
| 1K | +0.032% | -0.278% | +0.122% | +0.044% |
| 4K | -0.004% | +0.023% | -0.050% | -0.212% |
| 8K | -0.167% | -0.157% | -0.140% | +0.037% |
| 44K | -0.011% | -0.093% | +0.028% | -0.070% |
| 200K | +0.374% | -0.074% | -0.184% | -0.032% |

关闭态1K/4K TTFT、4K/8K decode的后续请求落在旧默认重复范围较慢一侧，
严格持平未通过，未追加有利采样。用户现已明确接受本轮实测性能，
3b414633已安装为默认；接受决定见上文，旧测量保持原样。开启态与关闭态
的后续范围重叠，也不据此宣称零开销；采集继续按需显式启用。
本阶段交付结束；专家offload、缓存执行器、长期采样均未做。
证据：.q4t-work/moe-trace-runtime-20260928/audited-results.json，
同目录保留原始HTTP、轨迹、故障注入、复用身份与候选包校验。

此前host格式基础已纳入本功能；早期仅合成样本的证据保存在
.q4t-work/moe-trace-foundation-20260928/，不替代上述真实运行记录。

## 此前入口治理：已完成并接受实测代价

实现e58e3f2已推送main，无额外分支；98a75fb4已替换默认d9d02f92。
ServerOptions是唯一配置源，能力组合由它派生；裸serve默认max_seq由8
改为1，MTP默认关、媒体默认关，显式开关与组合保留。严格解析整数与
有限比例，非法数值、范围、缺参在加载前退出2，不再静默截断或回退到8。
启动报告请求/实际能力；关闭媒体不加载vision，显式媒体仍加载原视觉模型。
保留原预算算法、max_len自动容量及max_prefill默认语义；裸入口启动时
预算给出262144容量，只验证了1024-token文本，不新增262K质量接受结论。
正式HTTP矩阵仍固定8192/208896，与原版预算报告逐项相同，不用省下的
视觉资源扩大容量。没有第二套runner、JSON配置或生成/调度/模型改动。

Thor零警告构建、34项host、14类实际CLI拒绝通过，无SKIP。默认及显式
媒体两种实际启动、单槽、精确文本SSE、正常停止通过；默认媒体拒绝400。
显式媒体仅检查初始化与文本，不代表媒体生成验收。公共CI通过30项host、
16项框架反例及4项审计检查。
[实现CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/36373380858)。
HTTP质量11/11、55类输入拒绝/恢复、取消8组、decode故障4组、prefill
读回故障2组、生命周期5组通过。41条evalscope原始记录、9条生命周期
SSE和2条入口SSE审计通过。模板/输入/生成源码身份相同，复用此前63条
模板专项；没有重跑未受影响的全部专项。106个GPU函数有1个GDN机器码
变体，实际新旧cubin精确匹配此前已验证的两份cubin（方向相反），复用
60例有限数值对照，不宣称设备代码全部相同或完整模型oracle通过。

候选98a75fb4与当时默认d9d02f92各一轮完整五档、每档三次、输出256token，
均与冻结参考一致。
下表decode取调和均值、TTFT取算术均值；首请求/后续请求另存原始证据。

| 输入token | Decode变化 | TTFT变化 |
|---:|---:|---:|
| 1024 | -0.416% | +0.171% |
| 4096 | +0.002% | -0.175% |
| 8192 | -0.081% | +0.061% |
| 45056 | -0.105% | -0.016% |
| 204800 | +0.160% | +0.080% |

1K及44K后续decode在较慢方向超出本次原版重复范围，未满足严格持平
出口；不追加采样，不修改参考，不从减少视觉加载推导吞吐。一次先候选
后原版不证明因果或稳定尾延迟。用户明确接受本轮实测代价，据此安装
已验收原件并关闭待决项；未满足严格持平出口的测量结论保持不变。
发布包已核对117项运行时源码、二进制及构建缓存，已激活，无常驻服务。
证据：.q4t-work/serve-entry-20260928/audited-results.json；同目录package/
为候选包，artifact-binding.json封存原始材料。未做：MoE采集/offload、
独立CLI治理、MTP/媒体生成验收、长稳复验、完整实验执行器拆分。

## 当前维护增量：两项改造完成，实测代价已接受

实现365f1f2已推送main，无额外分支；已接受d9d02f92并替换默认45df506f。
只推进Model设备内存owner和ChatServer职责拆分。ModelOwner不可复制/
移动，失败加载检查完成后释放部分资源，拒绝覆盖已加载模型；服务
停止并释放MTP借用后由owner释放Model，补上原服务缺少Model释放的责任。
独立CLI/历史实验调用者仍保留原API，未宣称全仓所有权迁移完成。
ChatServer拆为生命周期、HTTP、输入准备、调度、生成和指标实现文件；
入口2354→335行，生成882行，保留共享状态、锁、调用顺序与默认配置。
逐函数核对仅加载入口替换及输入准备提取；106个GPU函数机器码相同，
复用既有数值证据。无kernel改动、异步回收、Graph或实验分支隔离。

Thor零警告构建、30项host、TCP三类连接合同通过，无SKIP；公共CI
26项host、16项框架反例、4项证据审计通过。真实CUDA owner专项覆盖
空对象、部分加载失败/重试、分配失败、拒绝覆盖已加载模型；第40及
1500次分配失败均无存活分配，两次完整加载各1538次分配只释放一次。
HTTP质量11/11、模板63条、取消8组、decode故障4组、prefill读回故障
2组、生命周期5组通过。104条evalscope原始记录及9条正常SSE复核通过。
首次缺少网络声明头的构建失败保留；修复后运行时与验证期间身份未变。
[实现提交CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/36367178347)。

候选d9d02f92与当时默认45df506f各完成一次五档矩阵，每档三条、输出256token，输出均与
冻结参考一致。下表decode为三次调和均值变化，TTFT为三次算术均值变化；
首请求与后续请求分别记录，没有追加采样或修改参考。

| 输入token | Decode变化 | TTFT变化 |
|---:|---:|---:|
| 1024 | -0.083% | +0.430% |
| 4096 | +0.018% | +0.078% |
| 8192 | -0.038% | +0.161% |
| 45056 | +0.001% | +0.079% |
| 204800 | -0.109% | -0.041% |

1K/4K/8K后续TTFT、8K后续decode在较慢方向超出原版重复范围，故未满足
EVALUATION的性能持平出口；一次候选/原版对照不证明因果或稳定尾延迟。
用户随后明确接受本轮实测性能代价，据此关闭待决项并更新默认；上述
未满足严格持平出口的测量结论保持不变，未重复五档或修改参考。
发布包与115项运行时文件、二进制及实际构建缓存一致；部署检查见上文。

完整证据：.q4t-work/server-owner-20260928/audited-results.json；同目录
package/为候选包，artifact-binding.json为封存清单。未做：实验路径
隔离、MoE采集/offload、独立CLI迁移、媒体/MTP验收、长稳复验及独立
关闭scheduler的HTTP fallback验收。后续入口治理见上文；此处仅记录此前所有权与拆分的验收。

## 原首版收敛记录（历史e659a108，不代表当前默认）

**首版私有文本Runner已完成收敛并安装默认入口。** 版本
`text-v1-20260928`，二进制e659a108；单Thor、指定模型、greedy、
MTP关闭、max_seq=1，五档1K/4K/8K/44K/200K。范围和完整证据见
[首版收敛验收](RELEASE_TEXT_V1_2026-09-28.md)。

成组资源修复与发布工具交付：上传绝对期限、正文与在途预算、
独立控制代理、校验包、监督重启、升级及失败回滚。22项主机、
55类输入拒绝/恢复、11题质量、63条模板、15条五档性能、8组取消、
4组decode故障、10项生命周期/资源检查全部通过；连续30.03分钟
112条请求通过，RSS峰值增量12.55MiB。89条evalscope原始记录与
121条正常SSE复核，1167文件封存。测试脚本一次失败保留，
仅修复并重跑服务阶段，运行时/配置未变。

已替换含已知缺陷的旧默认b5f38e48，默认构建树已同步；发布包
`.q4t-work/releases/text-v1-20260928/`可激活，当前没有常驻服务。
五档decode相对历史变化−0.164%～+0.214%；这是必要修复的代价记录，
不声明严格零回退，旧8K ABBA−0.277%结论保留。正式参考已更新，
旧参考归档。模型/量化源码与设备代码未变，明确复用既有数值证据。

本轮固定清单已关闭。媒体/MTP、公网多租户、认证/TLS与业务SLO、
完整独立模型oracle及offload/数据流属于其他交付范围，不再延长
本轮收敛。后续方向列于文末，不表示当前已验收合同仍在待办。

2026-09-28阶段交接复核：发布包、当前src/include、默认二进制、
性能参考及1167个证据文件身份一致；已修正过期文档入口，未改运行时
或重跑推理。可从此版本进入下一阶段，详见
[基线交接](BASELINE_HANDOFF_2026-09-28.md)。

## 本轮维护结论：功能通过，实测治理成本已接受

2026-09-28：用户已接受`45df506f`的本轮实测治理成本，维护阶段验收
关闭；PR #1已并入main（a115cb4），已合并工作分支已清理。
不将其表述为性能持平，也不放宽后续改动的通用门槛。按用户后续授权，
已安装`text-v1-maintenance-20260928` / `45df506f`，正式参考保留。
此前基线交接是维护前快照；当时主线源码与该部署包的运行时摘要一致；当前增量见上文。

普通文本以ModelSequence为唯一提交状态，区分submitted/committed；
删除serve重复游标、PLE历史及prefill状态重建。默认模型序列API等待
检查完成后提交；serve显式deferred，复用锁内读回同步。失败可见，
未完成工作拒绝结束/重入；model_mu_与默认流同步保留，无Graph。
代码中没有JSON驱动的模型执行计划；未新增平行计划源。删除旧
EXECUTION_PLANS.md的282行提案及唯一索引，其他提案仅作历史材料。

公共Linux CI实际通过26项host合同、16项框架反例、4项封存审计检查，
无CUDA或私有夹具依赖；Thor发布30项（原22项加8项）通过，无SKIP。
封存工具从冻结清单和真实PASS取证，不再硬编码22项。空筛选与SKIP
拒绝规则保留并实际运行。最终构建零警告，现有测试调用者编译通过。
[公共CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/36343286134)。

本机Thor、同一候选：序列提交/完成/失败/重入/回收与同分块交接通过；
HTTP质量11/11、取消8组、decode故障4组、prefill读回故障2组通过；
生命周期5组、9条正常SSE通过，两轮并发输出等于顺序输出。
候选与原二进制各完成五档、每档三次、256输出token，共30条性能
请求；输出均与冻结参考一致。原始41条evalscope记录和9条正常SSE
复核通过。模型目录、reference/、kernel源码与MTP算法未改。

下表为本次候选相对本次原二进制的完整HTTP对照：decode取三次调和
均值，TTFT取算术均值；首请求/后续请求分别保存在证据中。

| 输入token | Decode变化 | TTFT变化 |
|---:|---:|---:|
| 1024 | -0.267% | +1.154% |
| 4096 | -0.135% | +0.123% |
| 8192 | -0.286% | +0.035% |
| 45056 | -0.044% | +0.124% |
| 204800 | +0.123% | +0.094% |

1K/4K/8K后续decode和1K/44K/200K后续TTFT在较慢方向超出本次参考
重复范围，**不满足EVALUATION的性能持平出口**。没有追加挑样本、
改参考或用长档收益抵消短档回退。一次候选后一次参考不能证明因果
或稳定尾延迟，未把变化归因于同步次数。用户随后明确表示“我可以
接受这个部分的治理成本”，据此接受该候选的已测代价并关闭待决项；
这是本轮特定接受决定，原始未满足持平条件的测量记录保持不变。

首轮新序列夹具混比全行/末行head失败，保留状态0差、logits171差
原证据；仅改成同形比较后通过，未据此修改运行时。新构建106个GPU
函数中1个GDN函数机器码不同（源码和构建选项相同，原因未确定），
因此未宣称设备代码完全相同。对两份二进制提取的实际函数做60例
有限域对照：327366144个输出、25067520个状态值逐位一致且无非有限值；
不扩大为完整模型oracle。首次构建警告及修复后的零警告记录均保留。

实现提交140bee6；验证期间105个运行时源码摘要无漂移。证据与逐次
性能数值：.q4t-work/runner-maintenance-20260928/audited-results.json，
同目录保留首次失败、命令、二进制/源码、原始响应和最终封存清单。
未做：完整D/P/S执行器、GRFrame闭环、异步回收、kernel重写、媒体/MTP
验收、维护版长稳复验；未独立覆盖关闭scheduler的HTTP fallback。
默认文本路径行为回归通过；本轮接受依据为上述验证及用户的成本
接受决定，不是提交/推送本身。决定另存
.q4t-work/runner-maintenance-acceptance-20260928/decision.json，
绑定原1117文件封存清单与候选摘要；原证据包未改写。
部署单独验证发布源码快照包含cmake/共享清单。首次停止探针误用
端口bind而报EADDRINUSE，保留原失败；改验进程退出和连接拒绝后
冒烟通过，运行时未改。未重建默认静态库，直接安装已验证包二进制，
build/q4t.release.json指向其真实构建缓存；未把新构建冒充已验收版本。

## 历史阶段结论（不代表当前部署或下一步指令）

文本服务合同与主机门禁202794eb已完成有界功能回归，但性能无回退
验收未通过，默认二进制b5f38e48及参考保持不变。未实现参数明确400，
媒体默认在解码前拒绝，89处跳过改为真实SKIP；16项框架反例、21项
主机门禁、55类HTTP拒绝/恢复、11题质量、63条模板及4组decode故障
通过。五档15条输出同参考，decode较历史低0.001%–0.082%；追加
一次8K ABBA发现decode低0.277%，按首/后续请求分层范围不重叠，
不能以总体范围重叠判无回退。101条原始HTTP复核、510文件封存。
GPU代码包逐字节相同，下一节点限定定位host/调度/构建布局相关的
8K差异，不重复选有利样本或恢复错误计算。详见
[服务合同与验收](SERVICE_CONTRACT_2026-09-27.md)。

前置修复已进入上述候选：[decode错误终态](DECODE_FAILURE_2026-09-27.md)
及[JSON/HTTP输入边界](INPUT_BOUNDARY_2026-09-27.md)分别保留140/179
文件的独立直接回归证据。旧默认部署仍含已复现缺陷，不代表生产可用；
源码修复成立、性能接受与商业发布必须分开判断。[完善度审计](PROJECT_READINESS_REVIEW_2026-09-27.md)
中的全局主机/媒体资源预算、统一升级回滚与业务SLO仍未交付。

独立控制入口已完成有界交付：数据/控制双Nginx进程分别拥有连接
预算；共享进程128连接对照的健康请求失败，双进程三轮耗尽中
15次健康及3次实际取消全部低于固定2秒门槛。数据reload、双侧
独立重启、200K输出通过，8条正常SSE审计、102文件终态封存。
这是可选双端口配置，未自动启用、未改b5f38e48或性能基线；没有
双进程长稳或生产P99证明。详见[独立控制报告](ISOLATED_CONTROL_2026-09-27.md)。

控制预算隔离与有界耐久验收完成：原共享预算导致监控慢请求阻塞
取消429的反例两次复现；本地Nginx模板已分离生成4/取消8/健康4/
指标4。指标满额仍可取消实际请求；取消自身满额保留429，空闲
上传超时后恢复。17轮/628.6秒混合负载、活动请求停机与重启恢复、
新配置200K代理回归通过，39条正常SSE审计、215文件封存。
未改b5f38e48二进制/性能基线，未启用公网服务；十分钟不是生产
长期稳定性或控制可用性SLO证明。详见
[控制预算与耐久报告](CONTROL_BUDGET_2026-09-27.md)。

后端监听b5f38e48已交付并部署：默认127.0.0.1，--host数值IPv4
显式开放指定接口；配置错误在模型加载前拒绝。三种真实监听、
七项错误配置、显式loopback代理矩阵、11题质量及五档15条输出
通过，211文件终态封存。decode较432fc489历史低0.02%–0.36%，
五档TTFT/decode范围重叠，非严格零回退证明。默认远程访问行为
有意改变；同机进程隔离不在此合同内。详见
[监听地址交付](LISTEN_ADDRESS_2026-09-27.md)。

真实Nginx代理准入阶段已完成有界验收：生成预算满额时超额429，
健康/取消仍可用；慢上传隔离、FIN/RST与首内容后断连、200K完整
输出和3次恢复通过。裸后端128连接满载取消503的限制实测保留。
本轮仅增加配置/验证工具，未改432fc489运行时或启用公网入口，
未测五档代理性能。下一步后端私有访问合同，再做长稳/恢复。
详见[代理准入报告](PROXY_ADMISSION_2026-09-27.md)。

请求级取消432fc489已交付并部署：排队/prefill/decode统一取消，
支持独立凭据、期限及FIN/RST，完成/取消仲裁和关闭安全回收。
直接竞争/TCP、完整HTTP取消矩阵、copy/sync与fatal accept故障
注入通过；固定11题和五档15条全文同参考，26条原始记录审计封存。
decode较2319历史变化−0.004%～+0.240%，五档TTFT/decode范围均重叠，
未检测到超出已测波动的回退，不证明严格零回退或工业级发布完成。
EOF现在取消请求，不再支持写半关闭等待结果；下述旧FIN限制为历史。
详见[请求取消交付](REQUEST_CANCELLATION_2026-09-27.md)。

长prefill确认连接失败的取消修复已部署2319b14a：RST在8192/45056
处结束，完成已提交GPU工作后释放槽位；直接TCP、9条生命周期
响应及固定11题HTTP通过。五档各3次性能采集完成，全文同参考；
decode聚合速度较历史低0.21%–0.39%，范围重叠但非同时段配对，
不声明严格零回退。普通FIN仍不能与合法写半关闭区分，MTP未实测。
详见[取消修复](PREFILL_CANCEL_2026-09-27.md)。

有界状态验收已收束：新增257-token、127/1/128/1同分块直接检查，
五个交接点状态/全词表末行logits逐位一致，history/position/stage、
有效文本RoPE、页表/缓存未写区与槽位隔离通过。无生产变更，零警告，
退出0。覆盖矩阵及未覆盖边界见[状态交付](STATE_ACCEPTANCE_2026-09-27.md)。
取消修复现已独立交付；不冒称整模型或严格性能无回退验收完成。

当前服务生命周期补验：1K/44K的A→B→A、两轮两槽位重叠、
首响应前及首内容后断连恢复通过；9条完整HTTP响应的文本、usage、
stop一致，槽位归还。copy/sync读回错误注入均500、释放槽位并
将服务置为不健康，后续503；终态证据已封存。未改生产代码/部署，
未重测性能；输出一致
不等于logits逐位证明。详见[生命周期验收](STATE_LIFECYCLE_2026-09-27.md)。

最新状态合同验收发现并修复序列API默认槽位写错：Begin绑定槽位1，
旧prefill/decode省略参数却访问槽位0。修复统一使用序列槽位并
拒绝冲突/越界；完整模型短输入状态/缓存/logits回归及复用检查
通过。HTTP固定11题全文同参考，11条原始记录与89文件已封存；
修复已部署。本构建未测五档性能，不声明性能无回退。详见
[槽位修复](SEQUENCE_SLOT_FIX_2026-09-27.md)。server现有调用显式
匹配槽位，不能据该API反例声称线上请求已经污染。


**完整E4M3编码修复已合入并部署，建立新的数值/性能基线。**
合成与捕获激活各341600组有限域接入测试：尺度、FP4和decode
alpha差异均0；检查点147456个标量正有限、49152对gate/up相等。
干净版26题全文同归档修复版：检索11/11、推理13/15。五档各三次
HTTP性能采集通过，67条原始记录审计与315文件终态封存完成。
详见[修复接入报告](E4M3_NUMERICAL_FIX_2026-09-27.md)。

历史兼容版独立推理14/15，原4K求和输出600440；修复纠正该题，
同时1K/44K两题变错。历史新120题82→80与5个损失复现保留，
但不能以语义分数否决已证明的编码修复或恢复错误计算。
本次接受限于已测格式/接入合同；未证明整模型数值正确或商用发布
就绪，性能也没有同时段旧版对照，不宣称严格零回退。

历史MoE路径已确认[248,432)编码为448及次正规舍入/进位缺陷。
原4K输入/中间量化分别有1208940/148339项高区间错误，覆盖48层。
本次修复共用编码函数；旧路径的条件矩阵乘一致不能抵消其尺度错误。

此前交付（2026-09-27，以下为当时判定，最新验收见上）：停止逐层扩展，完成两个MoE量化点
HTTP隔离及直接格式回归。原编码147项失败、仅高值27项失败，
此前完整修复33146项全部一致，形成可独立复现补丁包。两个新
运行时候选均新增1K错误且4K仍错，均不接受；已恢复原版本并
核对三题HTTP。编码修复可验证，默认部署仍未通过质量要求。
详见[有界交付结果](CORRECTNESS_DELIVERY_2026-09-27.md)。
此前离线传播止于layer2注意力后主干，证据保留，停止继续铺开；
见[尺度传播](MOE_SCALE_PROPAGATION_2026-09-24.md)。

此前完整QSA选择/attention指定算术相同；高精度保留概率与门控
BF16舍入后仍有175500项差异。以上均为实际输入上的条件参考，
不是从原始token独立生成整模型结果。两轮日志清单生成时机问题
已修正并保留原证据，后续均在写入进程终态后封存。

## 目标与工作规则

先完善runner，以保持精度、各档性能不回退为门禁，再逐步演进
模型专用数据流引擎。性能持平且净复杂度下降也接受；文档、封装
或生命周期缩短不能直接当成已测吞吐/系统峰值容量收益。

用户已授权自主推进、修正、回退及阶段commit/push main，无需
常规请示。模型目录与reference/只读，保护其他任务未提交内容。
每次有意义的改动更新本入口并向当天日志顶部追加，不改日志历史。

- 性能优化必要构建后先tools/evalscope真实HTTP，E2E通过后才做
  数值/计时/profile，不用旧bench。Bug修复可先用直接针对缺陷的
  单测、边界或差分；实现符合性与HTTP任务质量分开判断。
- MTP关闭，单流greedy，max_prefill=8192、max_len=208896。
  输入1024/4096/8192/45056/204800，每档固定同输入三次、输出256。
- TTFT包括HTTP/分词/prefill，不能称纯prefill时间。按重复范围比较，
  不临时加容忍百分比、不拼接最优档位、不反复采样寻找通过。
- 只读观测的首项为关闭/开启/关闭原4K HTTP：原始请求、全文、usage、
  stop及服务退出一致才继续定位。原错答保持只证明观测不变性；
  质量驱动退出1仍是质量失败，不能写成模型验收通过。
- 已绑定的冻结HTTP证据允许离线参考复用；不为纯离线分析重跑HTTP。
  新实验新目录，逐份绑定来源，全部差异保留，明确参考的设备依赖。
  存储规则见 [EVIDENCE_STORAGE.md](EVIDENCE_STORAGE.md)。

## 历史运行时与质量边界（截至2026-09-27）

本节至“历史性能参考”的旧部署、执行节点与数值记录保留作历史说明；
其中b5f38e48不是当前基线。当前以本文顶部、首版发布/交接报告及正式
performance_reference.json和metadata为准。

当前部署为请求取消与监听地址修复，build/q4t SHA256：
`b5f38e48ae9357e73d415d24ce5307038da5fa39f9eb297ad3678056c2dd3376`。
默认loopback，远程监听须显式--host。该二进制通过实际监听、代理
回归、固定11题及五档各三次性能；详见监听报告的测量边界。
旧432fc489及更早二进制/参考保留；默认build静态库尚未同步，
后续开发仍需必要构建。没有启用常驻服务或修改防火墙。



- 入口修复：固定11/11、独立14/15且独立15条全文同正式；永久入口
  HTTP工具63条及模板字节21/21通过。真实messages五档相邻45条
  长度/全文一致，三服务0，完整三次范围无不利分离。后两次仍有
  单侧不利项；未发现同时差于两侧参考，不证明严格零回退。
- E4M3合并修正a984c284：独立14/15→13/15，当时拒绝并恢复；
  现共用编码修复已接受，新部署/验收身份见上。
  高值单项0730db3b只测三题，父版2/3、候选1/3，不写成测过15题。
- 两个次正规候选43b0e688/e49bc64f：质量同父版，但性能门禁未通过。
  native次正规版8K TTFT三次范围同时差于前后父版，已拒绝并恢复。
  这些均不是当前运行时，不因低层参考通过而追认。
- 恢复HTTP三道变化题全文同父版：1K/44K正确、4K原错；这是有界
  恢复检查，不是新一轮完整矩阵。旧decode路径一致性实验亦未接受。

详见 [正确性恢复](CORRECTNESS_CONFIRMATION_2026-09-23.md)、
[尺度修正实验](E4M3_ROUNDING_2026-09-23.md)、
[高值单项实验](E4M3_HIGH_ONLY_2026-09-23.md)。

## 已有定位证据与尚缺什么

以下主要覆盖原4K单请求prefill。逐算子使用实际捕获输入，不能拼接成
“从原始token独立重算整模型”。高精度差异均保留；指定算术相同不
等于高精度相同，更不等于任务答案正确。部分末prefill/首decode
证据不能推广到后续全部decode、分块或其他上下文。

| 范围 | 已核对 | 未证明/限制 | 入口 |
|---|---|---|---|
| 模型入口 | 全请求embedding与四路展开；PLE查表SSD行及转换/缩放 | 非完整模型前向 | [查表](PLE_LOOKUP_REFERENCE_2026-09-23.md) |
| HC | 全4K残差、norm/mix、投影重放/FP64、门值及上下游交接 | 仍以实际主干为输入，非独立逐层传播 | [交接](HC_HANDOFF_REFERENCE_2026-09-23.md)、[投影](HC_PROJECTION_REFERENCE_2026-09-23.md) |
| Linear/GDN | 全输入投影、短卷积、q/k norm、零初态至末态全递推、NormGate/out_proj及残差消费者 | 指定精度参考含设备数学/cuBLAS；首decode与prefill精度合同不同 | [递推](GDN_PREFILL_REFERENCE_2026-09-23.md)、[输入投影](LINEAR_PROJECTION_REFERENCE_2026-09-23.md)、[输出](LINEAR_OUTPUT_REFERENCE_2026-09-23.md) |
| MoE路由/量化 | 全router/mapping/gather、输入及中间量化；E4M3错误确实到达实际计算 | FP64 router舍入后103行集合变化，未建任务因果；错误尺度未修正传播 | [路由](MOE_ROUTER_REFERENCE_2026-09-24.md)、[输入量化](MOE_INPUT_QUANT_REFERENCE_2026-09-24.md)、[中间量化](MOE_INTER_QUANT_REFERENCE_2026-09-24.md) |
| MoE专家/合并 | 全GU/down重放及FP64、路由合并、shared投影/SwiGLU及最终MoE合并 | 使用实际量化/路由输入；共享分支存在保留的高精度差异 | [GU](MOE_GU_REFERENCE_2026-09-24.md)、[down](MOE_DOWN_REFERENCE_2026-09-24.md)、[shared](MOE_SHARED_REFERENCE_2026-09-24.md) |
| PLE | 全prefill key/value投影、norm/gate/广播、卷积/历史及HC交接 | 逐算子条件参考，不是独立组合前向 | [投影](PLE_PROJECTION_REFERENCE_2026-09-23.md)、[卷积/门控](PLE_CONV_REFERENCE_2026-09-23.md) |
| QSA | 全query选择/KV/attention指定算术，主投影重放/FP64、norm/RoPE参考及实际消费者边界 | 完整indexer条件参考已补齐；未做独立整模型或高精度整链传播 | [完整QSA](QSA_FULL_REFERENCE_2026-09-24.md)、[旧局部范围](QSA_SELECTION_REFERENCE_2026-09-23.md) |
| 输出头 | 七次最终mixer、全词表logits、argmax、后续token交接 | 上游仍为实际主干，没有从原始输入独立生成 | [mixer](FINAL_MIXER_REFERENCE_2026-09-23.md)、[logits](OUTPUT_HEAD_REFERENCE_2026-09-23.md) |

参考依赖与接口见 [上游参考合同](UPSTREAM_REFERENCE_CONTRACT_2026-09-23.md)。
SGLang在本机的现有对照未形成稳定、可比的整模型oracle，不能据
“另一框架同错”证明本实现正确；旧ref_dump的精度路径也不能替代。

## 历史执行节点

槽位缺陷修复、真实HTTP生命周期及关键同分块交接验证已完成，
本轮有界状态阶段收束。长上下文稀疏路径、跨分块数值关系、视觉/MTP
等未覆盖项明确保留；请求取消已独立交付。真实代理与生成准入预算
的有界验证完成，默认loopback的后端监听合同已交付；控制预算
隔离与17轮有界耐久/后端重启恢复也已通过。双进程连接资源隔离
也已通过本地对照与reload/重启检查。后续商用
验收需真实业务负载、并发/队列预算与目标SLO，不能从当前有限
合成检查推导生产可用性；认证和实际部署网络仍未验收。
后续性能/复杂度优化以当前b5f38e48为测量基线，遵守五档E2E。


此前配对筛查（历史节点）：120 条新样本，原版 82/120、完整修复 80/120，3 改善、5 退化、
35 两版都错。5 道退化题双版本各复查一次，全部逐字复现；302 条
原始 HTTP 审计完成、终态封存。旧检索 11/11→11/11，旧推理 14/15→13/15。
新集总分、3 类任务与 2 档长度下降，按冻结门槛不进入性能验收。
该轮默认二进制未变、未做性能。这是有限合成筛查，不是总体质量证明。
详见[质量评估结果](QUALITY_REVIEW_RESULTS_2026-09-27.md)及
[冻结计划](QUALITY_REVIEW_2026-09-27.md)。

用户明确Runner对数值执行负责。历史筛查拒绝结论保留，
不再作为数值修复否决依据；完整修复的有限域接入与26题HTTP
核对已完成，五档性能采集通过。本次缺陷收口，不追加语义样本、
舍入组合或逐层传播。后续工程以已修复基线出发，仍按五档E2E。
全局异常输入fail-fast、状态/缓存/分块一致性及商用业务质量
属于后续独立验收，不能据本次局部通过冒称已经覆盖。

## 历史性能参考（b5f38e48）

当前五档参考为2026-09-27监听地址构建b5f38e48，记录在
[performance_reference.json](../tools/evalscope/fixtures/performance_reference.json)。
五档各三次、输出256、MTP关闭、单流；范围与比较见监听地址报告。
旧432fc489归档为performance_reference_pre_listen_address_20260927.json，
更早参考保留。新参考是实测身份更新，不是严格无回退证明。

| 输入token | TTFT均值秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.797 | 18.577 |
| 4096 | 2.627 | 17.885 |
| 8192 | 5.170 | 18.124 |
| 45056 | 30.451 | 17.860 |
| 204800 | 159.311 | 17.109 |

已接受工程基础包括GRFrame、normed复用/布局单源化、MoE暂存别名、
资源合同、GRWrite→GRRead融合、成对gate/mix、线性scratch所有权、
serve读回/提交边界、长文本分块调度及按消费者缩减输出头/logits。
这些不等于完整数据流引擎；各阶段验证与被拒绝实验见
[历史入口快照](HISTORY_STATUS_2026-09-24_CORRECTNESS.md)。
完整D/P/S、可执行计划和状态提交仍未实现，筹备见
[数据流项目状态](../dataflow-engine/STATUS.md)。

## 后续独立工作（不阻塞已冻结首版范围）

- MoE固定容量离线缓存回放已完成，结果与在线影子候选见上文；既定回放合同
  比较静态热点和简单动态缓存，
  遵守decode整组top-10、prefill块内专家并集和相同字节预算；先判断普通
  缓存收益，再决定在线影子统计或预测预取。受控采集已交付，真实offload、
  长期采样和专家驻留管理未做；下一会话范围与证据入口见
  [MoE路由分析与缓存回放](MOE_ROUTING_TRACE.md)。不为文档或纯离线工具
  重跑原HTTP矩阵，不自动开启运行时改造。

1. 随机文本的工具与服务端分词计数差异（目标1024，实际932/921/915）未解决。
2. 补真实语料、多轮及其他上下文质量；固定样本正确不代表全面长上下文召回。
3. 断连专项六轮恢复已通过；旧4K非确定性已修复。后续原4K独立求和
   错题在本次数值修复后正确，但1K/44K另两题变错；不能混同
   已修复的索引槽位/旧QSA数学问题。
4. 纯prefill时间尚未测量，TTFT不是纯prefill；细分析须满足现行门禁。
5. 约10.24GB/token、260GB/s对应25.4tok/s只是静态理想参照；没有
   当前完整时间账，不宣称decode已无优化空间。见 [静态预算](DATAFLOW_OPTIMIZATION.md)。

## 历史与证据

[本轮治理前完整快照](HISTORY_STATUS_2026-09-24_CORRECTNESS.md) /
[2026-09-21快照](HISTORY_STATUS_2026-09-21.md) /
[当天追加日志](log/2026-09-28.md)。专题报告与证据路径保留，历史
“运行中”“下一步”不再充当当前指令。全局索引仍见 [README.md](README.md)。
