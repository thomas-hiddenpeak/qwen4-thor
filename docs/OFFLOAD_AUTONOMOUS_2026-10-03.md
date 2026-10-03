# Offload自主推进结果（2026-10-03）

## 本轮结论

原定有界实验已完成：greedy_overlap重排在完整六档配对中为
NO_GO_FULL_PERFORMANCE_SCREEN；保持Q4T_MOE_CHUNK_ORDER=0默认值。
唯一后备min_new_csr_v1分区研究通过离线门槛，值得下一阶段接入验证，
本轮没有把它接入运行时，也没有接受任何新的默认部署策略。
54GB整体物理RAM目标仍为INDETERMINATE。

这是按冻结出口收敛的结果，不为用满用户约10小时窗口增加采样或调参。
数值/业务/生命周期D阶段以完整性能通过为前提，本轮未触发；
D工具的9项host合同通过不等于真实模型D验证通过。

## 范围、身份与协议

入口HEAD664ad059，工作分支codex/moe-residency-20260930。
模型/reference只读，GEMM与精度实现冻结，MTP和运行时Phase D关闭。
原7处未提交修改独立保留；干净源码导出不包含这些Phase D/统计修改。
运行时源码提交dba22c700a4cb46418f0d785b82d0e559578b118；
候选q4t SHA256：
`6ab2a8203407fb177b2377fbae29885c5464c5caf6d409bcc290b696685c2fad`。
原build/q4t与封存基线为46b3f977；最终交付、远端提交和原diff保护
证据保存于本机证据根的final-delivery.json及final-completion-audit.json。

证据根：`.q4t-work/offload-autonomous-20261003/`。下文JSON/日志路径
未带前缀时均相对于该目录；原始数据按项目约定留在本机，不放入Git。

重排只改变prefill子块执行顺序：按当前GPU槽位重叠最大选择下一块，
平局用原子块索引；token分区、每行top-k顺序、GEMM和scatter行号不改。
仅T>1且多子块时启用，decode不重排。固定C256/k10/T8192下新增
bitset数据至多21,384B，另有vector/allocator开销；无新增GPU分配/同步。
E2E包含选择器成本，但未独立测量该部分CPU时间。

首项测试为真实tools/evalscope HTTP固定11题（含200K），参考文本、
长度和容量11/11通过。该质量组使用继承缓存、不限host/cache，
不把其时间作为16GiB性能对照，也不推导全模型数值或任务质量通过。
之后的初筛和完整矩阵均使用相同binary、C256、L2=16、mirror K8、
max_open_shards=200、pread_merge=1、inline_load=1；max_seq=1、
max_prefill=8192、max_len=262144。每组独立服务、MemoryMax=16GiB、
swap=0，目标模型payload页缓存0起点，组内请求之间不清缓存。
采样为资源1s、GPU10s、文件缓存仅启动前/退出后；运行期缓存峰未知。

## 重排：完整性能未通过

45K初筛off/on各3次通过预先门槛：平均TTFT219.802→212.308秒
（−3.41%），decode调和均值7.150→7.224 tok/s（+1.04%）。
证据pilot-decision.json；初筛只允许继续完整矩阵。

完整矩阵off→on，各1024/4096/8192/45056/204800/261887三次；
末档输出257，其余256，总容量262144保持。36次请求的输出、prompt、
usage、finish、容量和身份合同全部通过，两个服务及其客户端进程组
正常退出，无live/zombie残留，unit已移除。

| 输入 | 平均TTFT off→on（秒） | 平均TTFT变化 | 最低decode变化 | 冻结门槛 |
|---|---:|---:|---:|---|
| 1024 | 10.149→10.179 | +0.30% | −1.47% | 未过：后两次TTFT、decode |
| 4096 | 25.463→25.519 | +0.22% | +0.22% | 未过：首次及后两次TTFT |
| 8192 | 37.955→37.255 | −1.84% | +0.11% | 通过 |
| 45056 | 220.021→212.826 | −3.27% | +2.26% | 通过 |
| 204800 | 1096.413→1054.651 | −3.81% | +0.97% | 通过 |
| 261887 | 1416.519→1362.023 | −3.85% | −0.91% | 未过：decode |

表中平均值用于描述，接受门槛始终为逐档：on首次TTFT≤off首次，
on后两次最大TTFT≤off后两次最大值，on三次最低decode≥off最低值；
六档必须全部通过。1K后两次最大TTFT9.851→9.933秒，最低decode
7.003→6.900；4K首次25.164→25.313秒、后两次最大25.623→25.655；
末档最低decode6.204→6.147。未调整门槛或追加有利重复。

这是一轮有界工程筛选，三个观测/档、固定off→on顺序；不能证明
统计非劣性、永久性退化或排除时段因素。只有整组首次1024请求紧接
payload0，其余档首次继承组内状态，不统称冷请求。长档TTFT收益
不能抵消未通过项。相对本轮off的改善未达TTFT−30%；没有本协议
同条件C0对照，不签收历史60%C0 decode目标。

权威结果full-matrix-decision.json，SHA256：
`51f47b158a66b9667c8cd4a78a295eff3abff004dd87b6abaa50c24f8f8d2c4a`。
逐次值保留在该文件；描述表派生自matrix-metric-summary.json。
initial-route-decision.json明确绑定合法NO_GO后才允许后备研究。

## 资源：读量下降，但口径必须分开

8条资源合成检查及四组raw审计首次通过，未修复或放宽审计规则。
105个消费源共126,607,218B均记录SHA；19,702个样本、42个请求
（初筛6+完整36）全部有读取计数上下界。最大边界不确定性0.9985s，
墙钟/单调时钟差约1.04微秒；未发现计数重置或身份代际变化。

| 完整矩阵输入 | 三次PID read_bytes之和减少 |
|---|---:|
| 1024 | 0.0754% |
| 4096 | 1.3585% |
| 8192 | 2.2166% |
| 45056 | 3.5492% |
| 204800 | 5.1635% |
| 261887 | 5.2375% |

259:1模型分区读取同方向，量级与PID相近；其上下界最多比PID高
0.02767%。这支持读取量级，不把全局分区归为q4t独占，不将父盘
259:0与分区相加。PID包括q4t全部文件读取；rchar及loader nvme_mb
属于逻辑读量。cgroup io.stat仍明显低计：完整off/on分别9/7条
请求的cgroup增量为0而PID为正，内核归属原因未知，不能据其签读量收益。

| 观测 | 完整off | 完整on |
|---|---:|---:|
| MemoryMax设置（B） | 17,179,869,184 | 17,179,869,184 |
| kernel memory.peak（B） | 17,180,733,440 | 17,180,749,824 |
| 实测超设置（B） | 864,256 | 880,640 |
| memory.events.max | 110,055,471 | 104,736,106 |
| OOM / swap | 0 / 0 | 0 / 0 |
| NVIDIA采样峰（B） | 56,237,228,032 | 56,237,228,032 |

唯一PID I/O缺测精确位于off sequence9211/shutdown，读取/proc/PID/io
得到errno13 Permission denied，PID/startticks/cgroup身份仍一致；
不影响42个请求的计数窗口。非PSI cgroup缺测仅启动前ENOENT，
两种PSI全程ENOENT，均保留UNKNOWN。GPU每组启动期一次无匹配进程，
启动前及退出后各一次无目标；其余未测值为计划跳过，不携带旧值。
GPU实际fresh最长间隔11.0155s，资源开始间隔最长1.0121s；
完整矩阵文件缓存仅端点0→约12.35GB，运行期未采样。

CUDA与既有共享缓存未被16GiB cgroup完整覆盖；NVIDIA、RSS/PSS、
pinned/shmem、cgroup、文件缓存存在重叠且峰值不同步，禁止相加。
运行期文件缓存与整体去重物理RAM并集仍未知，54GB仍INDETERMINATE。
源码分配估算C256/C192/C160/C128分别66.629/58.135/53.889/49.642GB，
均排除文件缓存，固定L2/mirror约3.578GB；不能凭C160估算批准54GB部署。

证据raw-resource-audit.json、raw-resource-summary.json、
ram-capacity-review.json、physical-budget-definition-review.json。
原始审计脚本audit_raw_resources.py SHA为
`035f1f840d00350a7b6db62e1b8ef7f62a5fda02406dae91395d291a67652da4`；
执行记录在raw-audit-execution-01/。其5s/10ms解释边界未改变性能门槛。
需要复核时，在项目根运行以下命令，输出使用新路径以保留原证据：

```bash
python3 -B .q4t-work/offload-autonomous-20261003/audit_raw_resources.py \
  --self-test
python3 -B .q4t-work/offload-autonomous-20261003/audit_raw_resources.py \
  --output .q4t-work/offload-autonomous-20261003/raw-resource-audit-recheck.json
```

## 唯一后备：分区本身通过离线筛选

按partition-plan.json仅运行一次min_new_csr_v1：两个冻结45K来源
各首个8192-token prefill forward、各48层，共96层；每样本/策略
重置到相同热点，按新增专家最少选完整top-k行，平局原C++ lex rank，
按构造顺序执行，不叠加重排。CSR访问与chunk重置行扫描预算32*T*k，
超限丢弃整个候选forward并回退原分区；预处理/桶操作等另计，不称
总指令或时间上限。每个样本独立要求GPU补载至少−15%、结构下界下降、
块数不增、零预算回退；所有门槛均通过。

| 样本 | GPU补载 off→候选 | 减少 | 块数 | 转换结构下界 |
|---|---:|---:|---:|---:|
| 主45K首forward | 70,077→36,096 | 48.49% | 984→356 | 65,245→31,610 |
| 业务45K首forward | 71,317→37,620 | 47.25% | 990→364 | 66,264→32,921 |

96次helper均rc0，无超时/错误输出；每样本回退层数0。两个48层
样本的分区计时约46.75/46.05→113.89/113.14ms，额外约67ms，
这是离线helper测时，不能当在线CPU成本；规划器向量payload峰
358,112B，不含输入输出/验证/allocator，也不是RSS。

结果仅为OFFLINE_GO_FOR_CONSIDERATION。没有回放整请求、L2/mirror
或真实SSD，也没有数值/HTTP/TTFT接受。新分区产生单token子块：
prefill中的runtime-decode补载计数4/7（原分区0）；不是实际decode
请求变差的证据，但表明dispatch形状会改变，下一步必须检验数值合同。

证据partition-run-01/decision.json，SHA256：
`5ca9b71cce30fa9dce2c2a17d8709a48cb03e3aca4c865d794f2c04889c4d011`；
身份、298个产物SHA及96次退出核对见
partition-run-01-controller/verified-summary.json。

## 验证与交付边界

共133项host/工具检查通过：selector8、协议/监控58、D工具9、
完整矩阵审计34、资源合成8、分区C++/Python各8。运行时与分区工具
构建零警告。后续58项检查、资源raw读取和离线研究均在07:52:36
完整性能控制进程终止后执行，未争用性能测量。

收尾仅调整三个新C++工具文件的超长行。实验时11份源码已封存在
partition-source-before-format/；原冻结partition-plan.json不改。
当前排版对应partition-delivery-plan.json。重新构建后helper与测试
二进制均与实验版本逐字节相同，Python工具未变，故复用16项检查与
唯一一次研究；无重复模型/trace实验。证据partition-format-identity.json。

D入口d-validation-entrypoints.json是早期计划快照，其中“5项未运行”
注释由实际lifecycle-host-contracts.log的9项通过记录取代；真实模型
D本轮不适用，未构建/执行q4t_tests，不把固定文本一致当数值证明。

冻结队列是A实现→B初筛→C完整矩阵→条件D或唯一E→F收尾。
首两阶段提交dba22c7、c88c6f3已推送；最终工作分支提交、远端一致性、
原7处残余diff和证据核验见final-delivery.json/final-completion-audit.json。
保留运行时开关默认0，不合并或部署未通过策略。

## 下一阶段建议

优先把同一分区算法作为默认关闭、可整体回退的候选接入，保持
chunk_order=0及其余条件冻结。必要构建后首测固定HTTP质量；重点
核验新块形状、单token分派和scatter对应的数值合同，再做同条件
45K筛选，只有通过才完整六档与业务/生命周期验收。
同时单独闭合54GB的覆盖口径与真实物理证据，保留PID/分区/cgroup
三种I/O口径。离线47%–48%的GPU补载减少只决定研究优先级，
不能预报同幅度的SSD、TTFT或RAM收益。
