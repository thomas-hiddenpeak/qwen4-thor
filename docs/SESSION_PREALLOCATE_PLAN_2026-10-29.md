# 唯一文件预分配候选：冻结执行方案

用户2026-10-29授权按[离线方案](SESSION_SAVE_WAIT_REVIEW_2026-10-29.md)
执行。总预算自03:17:57 UTC起，截止04:17:57 UTC，包含准备、构建和测量，
不延长。4服务/40生成HTTP/0新GPU数值fixture/1候选；CPU写入上界128MiB，
总逻辑写入上界192GiB。保留原默认6f37/5fdc，候选仅在隔离路径运行。

改动只含Save分段观测和一次fallocate(KEEP_SIZE)；单程序两路径，
Q4T_SESSION_PREALLOCATE=0为旧路径，1为候选（候选程序默认启用）。
两侧Q4T_SESSION_SAVE_TIMING=1。记录模型锁/池锁、ranges、预算/淘汰、
open、预分配、D2H API、padding、SHA、pwrite、publish、失败cleanup的
墙钟/线程CPU及余项；每请求一行，不改计算、同步和状态布局。
unsupported显式回退并使本轮优化NO_GO，真实分配失败不发布。

首项测试仍为tools/evalscope真实HTTP，之后才执行CPU合同；必要构建及
原固定64GiB直接I/O预处理不当作性能结果。整个GPU阶段由同一独占锁持有，
监控沿用已修复版本，所有服务结束后检查采样flush屏障。

每服务：131071主输入＋三个33274/49150/59986子输入，均256输出，
源自旧失败的repeat-code-8192材料；公共OFF/ON各一个主先、三子并发
事务，然后依原顺序恢复两个主会话（各16上限，要求精确回答44），每次
恢复前长槽都已被另一主会话覆盖。恢复请求继续使用完整prompt，精确
私有命中131323；不增加fresh推理请求，复用原独立状态参考，新增CPU合同
核对逐字节恢复及d0/pending。完整主子输出必须匹配冻结旧HTTP参考。

| 服务顺序 | head | 预分配 | 事务顺序 | HTTP |
|---|---|---:|---|---:|
| 0 | full | 0 | OFF→ON→两次恢复 | 10 |
| 1 | full | 1 | ON→OFF→两次恢复 | 10 |
| 2 | subset | 1 | OFF→ON→两次恢复 | 10 |
| 3 | subset | 0 | ON→OFF→两次恢复 | 10 |

所有启动空私有/公共池，每事务新ID/scope；共享开启时主写一个8192种子、
三个子只读。4槽容量208896,65536,65536,65536；max-prefill4096、
greedy MTP k3、suffix、默认完整或显式65536行BF16 head。不扫描参数。
先一次固定64GiB预处理并保留至阶段结束，计入墙钟/磁盘，不声称SSD稳态。

全部HTTP完成后做一次CPU存储合同：跨块和未对齐逐字节/d0/pending、
取消不发布、copy失败、短写、ENOSPC、EOPNOTSUPP回退、checksum、短读、
TTL/LRU与预算释放；故障注入仅在独立CPU程序的linker wrapper中。

接受规则事前固定：两head各自的两个主私有Save墙钟中位数相比原路径
改善至少1秒；同head每个公共模式的主＋三子事务不能变慢；候选各head
公共ON相对OFF均为净正收益；全部输出/恢复/分段闭合/CPU合同通过。
完整组统一作性能决定，不因单个中途数值重试；正确性、环境或预算失败
立即停。监控/竞争失败ENV_ABORT，仅停止自有进程。任何门未通过NO_GO，
不追加第二候选或扩样。

复用已通过A112五档/质量、S32两head重复收益及未变评分/目标-草稿状态
直接合同；缓存存储改动由定向HTTP和CPU覆盖，不重跑全套。原正式C缺失
项不虚构通过；新的局部结论与原发布NO_GO分别记录。通过后只按实际具备
的交付范围收尾；未具备完整发布条件不将研究程序替换默认。

产物、输入、依赖、构建cache及源码差分由
`.q4t-work/session-preallocate-20261029/plan.json`和identity.json绑定。
