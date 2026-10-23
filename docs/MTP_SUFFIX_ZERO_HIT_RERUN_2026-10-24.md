# 修复监控后的固定 k3 suffix 有界再验证

2026-10-24，性能优化定位。用户新授权：用修复后的监控重新进行有界验证，
继续定位子集45K/200K索引建立之前的TTFT下降与8170-token探针波动，
确认两head重复收益。本卡在新测试前冻结；旧26HTTP阶段仍为ENV_ABORT，
不恢复其有效性，不把旧法证样本并入本轮。入口提交8df773b，分支codex/suffix-k3。

## 身份、范围及复用

运行时仍为源码cb0fa73c3271e6fbce2cd9d282838de9643d2a3b构建的min32程序，
`.q4t-work/suffix-admission-20261024/build/q4t`，SHA256
`6a4d3d62c09d814a9342cc698e1523e1029d1d3aa3e36e1b80cf7a625a30b090`。
没有新运行时改动或构建。当前交付build/q4t的f356b2c9及sidecar422993b0保持；
显式BF16子集仍为sha2-startup-20261023/delivery-package/artifact，manifest
b63b0f25。完整hash、依赖和模型metadata由准备工具逐项冻结。

B1、greedy、MTP k3、max-prefill8192、max-len208896、输出256；完整head
默认、65536行BF16子集显式选择、suffix独立显式开关。min32/数字规则、
全局token ID、四行verify、checkpoint/extend及next_d0/next_g合同不变。
已通过22质量、86 host、10阶段合同、旧命中回放和两head取消恢复按相同
依赖复用；独立状态oracle仍只有原两层GDN范围，不扩成整模型任意输入证明。

监控工具SHA256 `473d0b472304cbea8a54bf5fb95fcf2fbea47c9b1f7c57aec8e5fa243fd42cbe`，
复用其11项CPU测试（包含自有zombie）；本轮不再跑单测抢占HTTP首测。
仅在同一pinned目录确认相同start_ticks且Z/X时，将io EACCES记录为退出；
未知/live身份、其他字段/错误或固定源失败仍ENV_ABORT。是否实际重现异常
分支单列，不把正常退出当覆盖该分支。

## 事前机制假设

H1：长prefill差异必须依赖本服务先前请求留下的索引/allocator/cache状态。
在常规8170→45044→204781外，分别让45K/200K成为新服务第一个生成请求。
首long若仍慢，排除“必须有前请求”；若不慢，只支持位置相关，不能据此
证明allocator故障。首long不等于机器冷缓存，仍保留ON启动分类表、进程
布局和自然缓存等差异。

H2：8170零命中差异来自host提交/间隙，或已覆盖的stream完成区间，而非
新增目标工作。先以正常HTTP和same-identity OFF A1/A2观察方向与规模，
最后两服务使用现有MTP/layer计时分段。事件区间可含host未提交造成的
stream空隙；wall≈event不等于GPU busy，更不是温度/频率因果。1Hz近邻
thread计数有新生/退出线程及端点缺失，不能当精确请求CPU时间。

离线背景见`.q4t-work/suffix-zero-hit-rerun-20261024/offline-mechanisms.*`，
全部FORENSIC_ONLY_ENV_ABORT：历史频率样本没有档位变化，较热A2的200K
反而更快，简单降频/单调温度解释不足；不是排除瞬态干扰或识别根因。

## 固定阶段A：56 HTTP / 12启动 / 5400秒

| 顺序 | 服务 | 请求顺序及次数 | 计时 |
|---:|---|---|---|
| 1 | full ON首门 | 8170×1 | 正常HTTP |
| 2 | subset OFF A1 | 8170、45044、204781各1，code×3、text×3 | 正常HTTP |
| 3 | subset ON B | 同上 | 正常HTTP |
| 4 | subset OFF A2 | 同上 | 正常HTTP |
| 5 | full ON | 同上 | 正常HTTP |
| 6 | full OFF | 同上 | 正常HTTP |
| 7 | subset OFF首45K | 45044×1 | 正常HTTP |
| 8 | subset ON首45K | 45044×1 | 正常HTTP |
| 9 | subset ON首200K | 204781×1 | 正常HTTP |
| 10 | subset OFF首200K | 204781×1 | 正常HTTP |
| 11 | subset OFF细分析 | 8170、45044、204781各1 | MTP_TIMING和LAYER_TIMING |
| 12 | subset ON细分析 | 同上 | 同上 |

第一项测试为真实tools/evalscope HTTP，首门通过后才继续。共46正常、4首long、
6插桩请求，0重试/额外预热/扫描。实际code/text输入744/475；所有材料均来自
已观察的旧有效admission第一条prompt，标记development_diagnostic，不冒充
未见验收集。每个响应核对同head旧有效完整输出hash、输入、256输出、length、
SSE及释放。重复场景由evalscope按3请求batch执行，完成batch后逐响应核对；
发现失败即停止后续batch，不声称能在同batch第二条发出前捕获第一条差异。
首long各case只有一个启动对，两个不同case的逆序不构成同case ABBA。

同服务三请求decode中位数用于描述性重复收益复核：完整head的code/text
各ON/OFF≥1.05；子集每场景B/A1、B/A2均>1且两个中位比的几何均值≥1.05。
共享一个B不构成两个独立块，同启动三请求不构成三次独立启动。保留所有
原值、首请求标志；首门/插桩不并入收益。目标未达不扩样，达到也不撤销旧NO_GO。

长探针分别报告TTFT、handler prefill阶段、首内容后窗口、decode、接受分布、
verify/普通tail/神经proposal/extend工作及suffix生命周期成本。samehead
工作量相同不等于耗时相同；A/A极差不转成允许损失阈值。三个长探针应零hit，
实际重新核对；普通decode与质量没有新增全量矩阵，只按旧身份及原范围复用，
本A不作五档接受或总体非劣结论。

固定自然页缓存、startup auto，不drop_caches、改亲和性、功率、频率或SSD策略。
完整驱动持同一共享GPU独占锁，覆盖服务间隙及收尾；只停止自有进程。
固定1Hz监控RAM、sysfs频率/温度、进程/线程stat/schedstat/IO及监控自身成本；
冻结可用源，缺项明确列明，不把不可用PSI写0。冻结稳定源读取失败、竞争、
监控过期、退出/身份/容量/输出失败或预算耗尽即停止，不中途改矩阵。

监控延续至服务退出和阶段身份核对结束，审计sampler join、最终summary、
postflight、零unflushed attempt和guard COMPLETED；最后服务退出之后应有
完整flush；最终文件核对之后最多等待5秒，要求一轮采样开始及flush均晚于
该屏障，等待仍计入5400秒预算。保留原始退出异常，不能提前停采规避原bug。
轮询不能排除短于
采样间隔的干扰。所有服务关闭、锁释放后统一分析；GPU阶段停止额外离线
分析/构建，只进行必要的状态与监控存活检查，减少本任务的额外CPU竞争。

已有layer计时每次RunLayers创建/同步/销毁event，包含T4 verify/T1 tail，
因此只作插桩定位。目标prefill按请求边界分离，应分别为1/6/25个chunk；
优先报告total及逐chunk值。embed起点在EmbedLookup之后，相邻layer事件
slot覆盖可将PLE/提交间隙计入邻段；各标签不是独立纯kernel分类。handler
与event覆盖不同，残差不称CPU时间。layer显示0.01ms、每行舍入界0.005ms；
MTP显示0.1ms、每行每phase0.05ms，配对总和取两边行数累加。首个有内容
SSE所在轮只提供发射顺序proxy，不是client TTFT精确切点。缺行或同步失败
记诊断不完整，不能丢弃后宣布通过。

## 条件B与截止

从本次A驱动state.started开始全任务上限25200秒（7小时），包含A后分析、
开发、必要构建、条件B及健康；不重置旧阶段记录。本A上限90分钟，预计
约45–55分钟，估计不保证完成。只有新有效证据支持具体局部修复才选至多
一个运行时候选；先冻结代码、受影响合同、收益/保护及新未见材料，再构建，
HTTP仍为第一测试。无具体机制则B_NOT_RUN，结束有界定位，不制造优化。

条件B最多346HTTP/18启动：两head各三个配对块（12服务），每服务新五档
各3、新代码/文本/非重复/数字4、开发长探针3及开发重复2，24×12=288；
两head质量22、同期交付/候选普通decode五档30、取消恢复6。直接受影响
合同最多1200秒。重复收益各head/场景各块正向且配对GM≥1.05；五档decode、
TTFT、开发探针、非重复/数字、普通decode、质量、状态及统一RAM分别保护。
不得以full收益抵消subset损失，亦不以命中率或同目标工作量代替净收益。
具体候选机制目标及完整B清单须在开测前再次冻结；预算不足不缩减保护。

A+B≤402HTTP/30启动；接受后的条件健康≤3HTTP/3启动、1200秒，全任务
≤405HTTP/33启动且仍受原7小时绝对截止。无候选不使用B预算。阶段终点
分别记录证据有效性、测量完整性、统计结论、工程决定；满足完整接受条件
才按授权回退包/默认程序/元数据/健康交付，否则保持f356及原显式子集。
原路径、候选和失败证据保留，完成文档、归档、阶段提交推送。

## 完成结果：有效复核，尚无可行动的运行时根因

2026-10-24本地06:29–07:17，阶段A **VALID / COMPLETE**：56HTTP、12启动，
2887.428秒。冻结工具提交4db6146，计划SHA256
`c8f66f2b1bb5e0fbd6e7adb10c8545a9a004ce9faaf1f529c33c841d6a279ec0`。
516冻结输入、798原始文件及模型metadata核对通过；raw seal SHA256
`b91d5e9d8ec3ecb92e8031e67698ec78e17b268d9410af92136d20c973ce0ebd`。
56条输出均与同head冻结参考一致，输入/输出/终止/释放及12服务正常退出闭合。
原22质量、86 host、10阶段合同只按原身份和范围复用，本轮没有重测或扩大证明。

修复后的监控真实闭合：2888/2888完整采样，0未落盘attempt，2645次NVML
查询未报告竞争。最后服务退出、最终文件核对之后仍有一轮完整新采样，
sampler join、summary、postflight和外层COMPLETED/child0全部通过。
实际遇到三次受控`EXITED_DURING_IO_READ`，包括最后q4t；均为同一pinned
身份确认Z态后的io EACCES。其他错误没有被忽略。61个历史自有PID及
start_ticks经一次只读核对均无匹配残留；没有停止其他会话进程。

[独立审计](evidence/mtp-suffix-zero-hit-rerun-20261024/audit-rerun.json)、
[收尾复核](evidence/mtp-suffix-zero-hit-rerun-20261024/compact-closure.md)、
[紧凑原值](evidence/mtp-suffix-zero-hit-rerun-20261024/compact-results.json)
保留完整身份与范围。旧26HTTP仍是ENV_ABORT，没有合并样本或更改旧决定。

### 两种head重复收益均复现

下表为普通HTTP、同服务三请求decode中位数，单位token/s。子集两侧共享
同一个ON；GM只作事前冻结的描述性复核，不产生两个独立块或统计置信度。

| head / 场景 | OFF中位 | ON中位 | 增益 |
|---|---:|---:|---:|
| 完整 / 代码 | 41.805 | 47.747 | +14.213% |
| 完整 / 文本 | 42.411 | 48.576 | +14.536% |
| 子集 / 代码 | A1 47.367 / A2 47.250 | 50.525 | +6.667% / +6.932%；GM +6.800% |
| 子集 / 文本 | A1 45.826 / A2 45.940 | 48.766 | +6.415% / +6.151%；GM +6.283% |

全部达到本轮开发材料复核目标。12条ON重复请求每条54次suffix命中，
共648次，接受分布[0,0,0,648]；代码两head和完整文本命中率54/63=85.71%，
子集文本54/66=81.82%。12条ON长探针均零命中。总计24条ON请求1645次
查询；该混合总命中率不作为收益判据。重复请求仍执行63次verify/63次extend，
子集文本66次；接受前缀extend每请求252行，状态链未跳过。完整与子集的
净收益分别成立，不能据此撤销长上下文保护问题。

### TTFT：45K方向变化，200K仍未闭合

| 子集对照 | 45K TTFT ON−OFF | 200K TTFT ON−OFF |
|---|---:|---:|
| 常规顺序，ON对A1 | −4.054ms | +275.549ms |
| 常规顺序，ON对A2 | +13.622ms | +68.389ms |
| 新服务首long | −41.895ms | +34.276ms |
| 最后插桩对，仅诊断 | −27.804ms | +36.829ms |

正常200K的目标prefill完成事件差为+264.826/+66.019ms，主要差值仍在
当前索引建立之前。A2相对A1自身TTFT也增加207.160ms；这说明同身份基线
有明显位置/时间差异，**不是**自动允许损失207ms或证明ON非劣。
首200K的prefill完成差为+14.379ms，其余TTFT差不能直接命名为索引或CPU。

首long45K不复现慢点，首200K仍有小正差，不能用“必须由前一个请求索引
残留造成”统一解释观测，也不能证明原常规200K差异的allocator因果。
每case首long仅一个启动对，不能把正差都视为同一原因或把方向变化当修复。

[独立机制复核](evidence/mtp-suffix-zero-hit-rerun-20261024/causal-review.md)
进一步排除了一条容量/分配路径解释：ComputeMemoryBudget使用MemTotal，
且在248320字节分类表分配之前；同head所有服务预算报告、有效208896/1/8192
容量和完整startup allocation plan一致。子集均1608项/97023144328字节，
完整均1606项/96687337864字节，策略均stable_descending_bytes，consume/free
闭合。没有分类表触发容量、专家slots或分配策略分叉的证据；物理地址与
缓存布局没有被这些计划观测，不能全部排除。

插桩200K target-prefill handler区间差+29.755ms，layer event总差+29.170ms，
残差差仅+0.585ms；事件分组full +99.09ms、lin −69.69ms。45K event总差
−23.070ms。差值主要处在已覆盖stream区间，但它可以包含host提交空隙，
不能据此宣布GPU算子、PLE、温度或CPU调度已被识别为根因。各chunk和
计时精度界见[分相原值](evidence/mtp-suffix-zero-hit-rerun-20261024/rerun-analysis.md)。

### 8170-token探针：子集小幅负向仍在，额外目标工作没有回来

正常完整head ON/OFF decode +0.174%；子集B/A1 −0.634%、B/A2 −0.238%，
OFF A2/A1自身−0.397%。子集首内容后增加41.988/15.766ms，而query+append+
release仅0.103508ms；完整对应0.124497ms。索引Init在首内容前，不能拿它
解释post-TTFT差。所有同head臂工作一致：完整75次verify/0普通tail，子集
74次verify/3普通tail；不存在本轮ON新增一次尾部forward。

最后插桩子集仍−0.292%，post-TTFT多19.365ms。排除首次有内容SSE所在轮
的发射proxy后，native三phase合计多20.5ms，但其舍入界±21.9ms，不能精确
解释19ms。单独neural draft wall多13.3ms（逐phase差舍入界±7.3ms），
同proxy内target layer event反而少4.19ms（±0.76ms，包含三个普通tail）。
这把继续观察的范围指向同一神经draft/提交区间，而非更多目标forward或
直接suffix lookup；它仍是插桩单对，不能升级为未插桩请求的已证根因。

### 匹配与统一RAM成本

所有零命中请求init+query+append+release合计均≤0.986136ms，重复请求
≤0.280172ms。正常ON每服务9请求合计子集2.889766ms、完整3.100446ms。
分类表在服务ready之前初始化，正常两head分别19.422/19.089ms；释放分别
0.001777/0.002463ms，均计入资源记录，不能直接归到随后HTTP的TTFT。

请求索引payload峰值1884160字节、对象248字节，共享表248320字节；总逻辑
请求量2132728字节。它不是独立物理RAM增量。全阶段统一MemAvailable最低
16144080KiB；正常请求窗口最低子集OFF A1/ON/A2为18758368/18766764/18771732KiB，
完整OFF/ON为16167784/16144080KiB。RSS、GPU、managed、文件缓存属于重叠
物理DRAM视图，不相加、不把这些小差当纯索引分配成本或新总体RAM非劣证明。

采样累计collection/encoding/flush墙钟68.161秒，其中写入/flush1.054秒；
不是监控CPU时间，也不从前台耗时扣除。固定53源可用，PSI三源从冻结即不可用。
长探针所采GPU/内存及CPU频率没有档位变化；较热A2的200K反而比B快，
简单温度单调解释不成立。首45K OFF近邻IO/major faults高于ON，窗口有spill，
不能把首long差直接归于索引或页缓存。新request线程在前边界不存在，
common-thread差遗漏关键prefill线程，不能伪称精确CPU归因。
1Hz读数和未记录的短暂干扰仍有边界；完整锁/轮询不能证明任何瞬时竞争都不存在。

### 工程决定

| 字段 | 结论 |
|---|---|
| 证据有效性 | VALID；修复后的真实收尾通过，旧ENV_ABORT独立保留 |
| 测量完整性 | 阶段A 56/56 HTTP、12/12服务、native/layer/监控完整；条件B/健康未执行 |
| 统计结论 | 开发重复目标两head均达到；共享B、同启动重复和单对不支持总体非劣，200K/子集8K保护仍未闭合 |
| 工程决定 | STOP_NO_ACTIONABLE_RUNTIME_CAUSE_KEEP_DELIVERED_F356；未找到足够具体的可修复机制，不选择运行时候选 |

预算未耗尽，但没有机制支持条件B，不用余量自动扩样或扫描。运行时改动0、
新构建0、新运行时候选0；没有将查找微优化包装成已解决百毫秒prefill差异。
当前交付f356、sidecar和显式BF16子集保持；没有部署或条件健康请求。保留
min32研究候选、原路径与完整失败/成功证据，完成归档及研究分支提交推送。

当前codex/suffix-k3仍有独有研究提交，保留该分支及证据路径，不做重复入口清理。

独立归档`archive-closed`共1279条目/208394299字节，逐文件复制及独立回读
核验通过；manifest SHA256
`a7d93df0bf429e0699137c0b15bd6152b3da5a3f3ecbe136b4c867705adb4d8e`，
seal SHA256 `96254073bd6c48d476137ae42715d3e5232b4c41cd78e1bdb37498f629c6cfec`。
包含受测6a、交付f356/sidecar、子集manifest、冻结快照、raw、审计、分析及
收尾文档封印前副本；源路径不变。后续本段seal文字不递归纳入自身归档。
