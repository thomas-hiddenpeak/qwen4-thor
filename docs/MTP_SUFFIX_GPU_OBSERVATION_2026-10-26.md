# 固定k3 suffix：退出终态修复与GPU区间对照

监控与两项工具修复已通过有界CPU验证；8K OFF/ON已取得可局部使用的
GPU记录对照。200K ON在报告收尾触发冻结日志容量上限，整阶段ENV_ABORT，
200K OFF未启动。工程决定保持**PERFORMANCE_NO_GO_UNCHANGED**：不安装
suffix候选，不改默认或发布元数据。完整交付目标仍未完成。

## 本次具体修复与预算

此前[目标优先退出阶段](MTP_SUFFIX_TARGET_FIRST_2026-10-26.md)已将报告绑定后
的正常顺序改为目标退出→shutdown，但CLI的“No active sessions”被一律拒绝。
本次保留原始rc1，仅在已绑定报告、同身份pidfd退出/目标收据rc0、launcher
rc0、已知子进程回收完整、精确有界控制记录均成立时，另外标记
OWNED_SESSION_ALREADY_ENDED。其他错误仍拒绝，未改采集错误规则。

随后实际导出暴露路径分派缺陷：旧工具以完整合同字典相等判断同步采集，
新增清理字段使其误找trace.nsys-rep，漏掉已绑定的trace.1.nsys-rep。
导出现在显式接收报告绑定目录，并在导出前后重新核对绑定内容。旧调用默认
行为保持；坏绑定、变化的报告不得退回旧路径。没有修改推理源码或重建模型程序。

| 阶段 | 事前范围 | 实际结果 |
|---|---|---|
| 已结束会话分类 | CPU≤22方法/2suite/120秒；4启动/6HTTP/4200秒/0重试 | 20方法首suite通过0.325269秒；81.039983秒首8K正常退出/流封存，导出路径失败；后3组未启动 |
| 显式报告绑定 | CPU≤22方法/2suite/120秒；旧报告导出+分析一次≤240秒，门过才新四组；同一外层锁≤4800秒 | 首suite旧mock缺新增关键字失败，修复后22方法通过；两suite共0.601096秒；旧报告84.102014秒通过受限门 |
| 新GPU对照 | 8K OFF→ON→200K ON→OFF；200K各有固定8K前驱；4启动/6HTTP/4200秒、单步900秒 | 3启动、4实际请求、3完整HTTP收据；8K两capture完成；200K ON失败；新阶段835.125069秒，含旧报告的外层922.337269秒 |

两阶段均冻结7200秒工作墙钟。自然继承页缓存，不清缓存/改频/绑核，不增预热。
流仍为level1/window20/单线程/checksum，逻辑8GiB、压缩256MiB、固定工作区
12.25MiB、worker AS64MiB。清理独立60秒，其中流完成≤10秒。
CPU第一次失败、原路径分派失败及所有原始状态均保存，没有覆盖成通过。

## 8K局部结果

同一观测binary `f0ff0fe8…`，8170输入/256输出，输出SHA
`66bb6d8c428668a991608e634c39eabebd85918b0cdd400c7b22919214ad436e`。
两侧74次T4 verify、148次神经草稿T1、74次extend/252行、22次restore、
接受分布[7,8,7,52]及3次推导尾部T1相同。记录的kernel/memory签名计数与
bytes没有差异。ON为74查询/0命中；没有新增尾部forward。

| 指标 | OFF | ON | ON−OFF |
|---|---:|---:|---:|
| 插桩HTTP TTFT | 5995.100 ms | 5931.023 ms | −64.077 ms |
| 插桩HTTP decode | 37.8366 tok/s | 37.9258 tok/s | +0.23565% |
| 应用GPU已记录活动数量 | 278842 | 278842 | 0 |
| 应用GPU活动区间并集 | 11914.740 ms | 11912.323 ms | −2.417 ms |
| target prefill host exclusive | 5276.717 ms | 5216.495 ms | −60.223 ms |
| target prefill线程CPU exclusive | 1104.176 ms | 1058.197 ms | −45.979 ms |
| target prefill GPU活动时长之和 | 5913.153 ms | 5912.714 ms | −0.439 ms |
| 后73轮神经proposal host exclusive | 415.092 ms | 404.387 ms | −10.705 ms |
| 同区间线程CPU exclusive | 126.232 ms | 111.956 ms | −14.276 ms |
| 同区间GPU活动时长之和 | 501.813 ms | 498.730 ms | −3.082 ms |
| 后73轮accepted extend host exclusive | 392.399 ms | 390.769 ms | −1.630 ms |
| 同区间GPU活动时长之和 | 341.340 ms | 341.923 ms | +0.583 ms |
| 后73轮verify（含其PLE子项）host exclusive之和 | 5577.187 ms | 5576.277 ms | −0.910 ms |
| 同区间GPU活动时长之和 | 5482.815 ms | 5493.771 ms | +10.956 ms |

本对照没有重现8K下降。约60ms的prefill host差主要位于target_prefill_call
exclusive（−31.202ms）和PLE reader（−29.015ms），GPU活动工作量时长和
变化远小于host差。后73轮verify自身exclusive +64.826ms与PLE reader
−64.656ms基本抵消；不能把一个嵌套区间增加当作整段退化。

神经proposal后73轮的CUDA API数量也相同：15170次launch、584次
cudaMemcpyAsync、1460次malloc/free async、146次stream sync。launch调用
时长和为60.456→54.307ms，malloc async为38.927→33.461ms，MemcpyAsync
为290.364→293.156ms，显式stream sync为0.440→0.400ms。这说明只看显式
sync会漏掉大量API区间成本；它们包含驱动处理和可能阻塞，不能称为纯提交，
也不能直接当GPU执行时间。没有据此发现可归因于suffix的具体运行时缺陷。

ON索引init/append/query/release分别0.172533/0.062133/0.025166/0.013546ms，
合计0.273378ms；numeric 0.000518ms嵌套于query，不重复相加。索引payload
1884160字节、对象248字节；共享分类表248320字节另列。这些是本次插桩
样本的成本，不能充当普通运行的精确增量。

上述GPU时长之和是工作量时长，可能重叠，不是elapsed；各分组并集也不能
相加。host wall减线程CPU只表示线程未运行墙钟，不等于GPU等待。所有结论
限于一次插桩配对，不证明原下降消除、正常性能非劣或唯一因果。

## 完整性边界与200K失败

旧保存报告与新8K两组均通过原受限门，原strict仍FAIL，各保留6381条实参
未知的无对应memory activity提交、六条未解决诊断及通用CUDA/NVTX warning。
未用历史8K参数账本跨进程填参。三份完整NVLOG均完成逐行解码、EOF/帧及
逻辑/压缩SHA核对；没有再出现此前四条session连接/通知错误。这支持目标
优先退出的局部改善，不证明整trace零丢失，也不证明通用warning全属误报。

200K ON的模型端已输出256 token，74查询/0命中、接受[8,7,5,54]；索引init
1.046953ms、query0.037719ms。但同步报告收尾期间NVLOG达到逻辑6769869119
字节、压缩268433965字节后，再写会超过256MiB，worker以INVALID退出，
没有完整帧/EOF/SHA证书。不能把客户端未完成的响应算作完整HTTP或GPU样本。

容量失败发生时HTTP客户端仍等待profiler收尾，当前主线程检查不能及时
观察worker退出。根驱动核对当前步骤PID/start_ticks/命令并通过pidfd发送
SIGTERM，只停止自有步骤。原容量收据和触发依据保留；shutdown控制超时后
被TERM，目标/launcher最终为−13（SIGPIPE），不能称正常退出。已知子进程
完成回收，但这不修复测量失败。原state STOPPED、postflight ENV_ABORT、
guard COMMAND_FAILED保持；不扩容、不改压缩参数、不启动最后一组。

这揭示两个具体工具限制：200K所需日志超过原容量设计；worker失败在阻塞
HTTP区间没有及时传递。当前阶段不继续改动或加采样。下一步若继续，须先
有界解决失败传播和日志量方案，并用保存证据/CPU证明，再讨论200K采集；
不能只把容量调大后重跑，也不能拿8K收益替代200K保护。

## 资源、证据与交付决定

新阶段836/836监控落盘，pending0，无监控错误；39个历史自有身份终审均
ABSENT，锁释放。整机MemTotal−MemAvailable峰116834922496字节，swap峰
2147479552字节；PSI三源不可用，最大采样周期56.634ms。轮询不能排除短于
采样间隔的干扰；全机proxy不能和device/RSS相加，也不是模型或采集器增量。
三个有效流分别746312316→30454798、745669904→30484294、746499429→
30499513字节。失败流maxRSS53236KiB，固定工作区不代表全部RSS。

| 决策字段 | 结论 |
|---|---|
| 保存完整性 | PASS；新阶段25023检查/6.514231秒，原失败不改 |
| 测量有效性 | 8K局部FORENSIC_RECORDED_ONLY；整阶段ENV_ABORT_COMPRESSED_LOG_CAP |
| 测量完整性 | 8K记录配对1；200K配对缺失；4请求发起/3完整HTTP收据 |
| 统计结论 | 一次8K描述性配对；完整矩阵不完整，无正常性能/非劣结论 |
| 工程决定 | PERFORMANCE_NO_GO_UNCHANGED，不部署；完整目标保持active |

默认binary SHA仍为
`f356b2c96330101abf1e5fa3fced495a37f4ea5a270ed4457037e0d55735d313`，
sidecar `422993b0…`。min32参考`6a4d3d62…`、观测`f0ff0fe8…`、子集manifest
`b63b0f25…`及数学依赖未变。原250 HTTP/18启动五档已测齐但保护未通过；
22/22质量、有限独立接受前缀/状态合同、两head重复收益直接复用，不重测。
既有新重复场景full +14.028%/+14.607%、subset +6.948%/+6.448%；本次没有
用插桩数据或完整head收益替代子集净收益。覆盖边界仍见
[完成矩阵](evidence/mtp-suffix-contract-20261026/completion-matrix.md)。

已结束会话阶段529文件75198587字节归档，manifest
`8db920e43562ca108be278b17c04e5fc745e85ed5968d726d93ba75e15f83a81`；
本阶段1143文件717732904字节归档，manifest
`6072140b4565502cba7d02b5c993e5b35ab54a735dcc87f5ef9bb052051ec349`。
这些是依赖历史工具包/原路径的独立增量研究归档，不是已通过的发布包。
[证据索引](evidence/mtp-suffix-gpu-observation-20261026/index.json)绑定scope、plan、
CPU首失败/修复、退出、配对、独立审计与8K派生结果。阶段提交推送工作分支，
不因归档或工具修复完成而将整个suffix目标标记完成。
