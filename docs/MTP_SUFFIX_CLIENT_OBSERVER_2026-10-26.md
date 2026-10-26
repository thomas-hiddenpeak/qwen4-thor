# 固定k3 suffix：阻塞客户端期间的采集器失败传播

2026-10-26，48项CPU测试首suite通过，驱动墙钟0.776087秒；0模型加载、
0HTTP、0GPU。修复只用于诊断客户端等待路径，未改变推理、日志容量、
错误规则或普通验收默认路径。200K GPU对照仍未完成，性能NO_GO和默认版本保持。

## 已修复的具体路径

上一阶段原生采集器触发256MiB压缩上限并退出，但evalscope客户端仍阻塞
等待报告收尾。采集器检查位于run_cases调用前后，无法在subprocess.run
等待期间执行，最终由根驱动按身份通知步骤退出。原始ENV_ABORT与
SIGPIPE/控制超时证据继续保留，见[GPU区间结果](MTP_SUFFIX_GPU_OBSERVATION_2026-10-26.md)。

run_case新增可选process_check，由GPU配对驱动通过admission.run_cases传入
原MeasurementGuard.check。只有此诊断路径改用Popen等待，每次最多等待
100ms后重新检查，并在启动前和客户端退出后检查。采集器失败仍由原guard
锁存为ENV_ABORT；异常原样向上传递。100ms是轮询间隔，不是硬实时响应保证。

失败时只对直接拥有的客户端Popen执行TERM/等待2秒，必要时KILL/等待2秒；
不按扫描结果发信号，不对模型或其他进程发信号，不在清理时重调已失败检查。
独立client-process.json记录PID、实际退出码、检查次数和信号。清理或收据
写入再次失败也保留原异常；后者附加异常说明。未传process_check时仍使用
原subprocess.run，因此普通性能验收调用未增加轮询。

48方法包括22个GPU驱动合同、20个原HTTP runner合同、6个新增测试。新增覆盖
启动前拒绝、退出后的观察失败、TERM/KILL边界、清理/收据二次错误、参数逐层
传递，以及真实原生worker的小容量失败：在客户端等待10秒时制造压缩超限，
断言3秒内返回失败、自有客户端−15、旁路进程仍存活、原ENV_ABORT不被清理
覆盖。这里的3秒是被测CPU场景上界，未声称GPU重载时必然同延迟。
437源身份未变、无跳过/遗留临时文件。复用既有native binary及构建证明，未重建。

## 200K日志量的保存材料诊断

只读取上一轮压缩原件，SHA
`1ef08ec611b719648b0f967641c43b5b90e625000a921eb52317fc840b7a5717`。
首次有界解码得到6769737728字节并如实报truncated zstd frame，但分类器错误
使用带空格的pipe分隔，将所有行列为UNPARSED；该结果不能用于分类结论。
为这个明确解析缺陷单独冻结一次≤29秒修复核对，没有新增采样或第三次扫描。
修正后在时间上限处停止，覆盖4517789696字节/12543751完整行。两次合计
57.465115秒；第一失败、第二超时及原件身份均保留，不授予完整日志证书。

| 已检查前缀类别 | 行数 | 字节 |
|---|---:|---:|
| quadd_verbose_daemon_evtsrc_trace_event_details / ReadStorageImpl | 6244166 | 2963313897 |
| quadd_verbose_daemon_evtsrc_trace_events / SerializeEventImpl | 6244154 | 1542222173 |

两类合计占已检查字节99.7288%，集中在报告导入时的逐事件详情与序列化确认。
这是4.52GB前缀的观察，不能外推完整200K日志总量。ReadStorage详情中包含
完整性判定需要的终止事件；另一个logger也可能输出必须保留的过滤记录，
因此不能简单关闭整类logger。

下一步可检验的最小日志方案是只筛掉格式精确、没有payload的
`SerializeEventImpl: Trace[…] Serialized event with timestamp {…, …}`确认行。
源码核对表明现有terminal parser不依赖这类确认行；所有ReadStorage详情、
错误/未知记录、显式丢失、Stop与结束过滤记录必须保持。该方案**尚未实现或
验证**，不能先宣称能容纳200K。若实施，需在新冻结范围中证明筛选谓词、
保留原始输入SHA/计数、明确“筛选视图”的行号和SHA边界，用已有完整8K日志
核对同一终态/错误结果，再决定是否具备一次有界200K采集条件；不改压缩
参数或直接增加容量，不将筛选视图冒充完整原日志。

## 保存与决定

独立终审1111项检查通过，2.468143秒；460文件5694366字节独立增量归档，
manifest `3666a9430954fd0ddff966b87ecda139f685f3bc5d103dbcc9c3fa9e74b0b6ad`。
[证据索引](evidence/mtp-suffix-client-observer-20261026/index.json)绑定CPU、
两个保存材料解析结果、具体解析修复预算、源码变化及终审。归档继续依赖原
失败文件及历史工具包，不是发布回退包。

证据有效性为CPU_VALIDATED_NOT_GPU_VALIDATED；性能测量没有新增样本，
完整性仍缺200K配对；无新的性能统计结论，工程决定RUNTIME_NO_GO_UNCHANGED。
默认f356b2c9…/sidecar422993b0…、min32参考6a4d3d62…、观测f0ff0fe8…及
子集manifest b63b0f25…身份核对未变。两head既有重复收益、正确性/质量及
8K局部对照复用，不重复已通过试验；完整目标保持active。
