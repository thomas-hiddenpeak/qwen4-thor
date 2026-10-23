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
