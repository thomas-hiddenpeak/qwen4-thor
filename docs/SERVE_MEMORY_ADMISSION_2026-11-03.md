# Serve 统一内存准入保护（开发合同）

范围：业务可靠性修复，不是性能优化；父版本e67ad06e，默认程序
ed672a65/sidecar67897c81保持，未验收的候选不得安装。

已确认缺口：MTP缓冲预检把MemAvailable读取失败的0当作可继续分配；
服务的总请求、队列和body计数上限没有全局内存压力准入状态。

实现合同：

- `/proc/meminfo`有界16KiB读取，MemTotal/MemAvailable严格kB整数、
  唯一字段、溢出和截断校验；有效0与读数失败分开。MTP可选缓冲读数
  未知或不足时不申请，普通路径回退plain；必须有MTP缓冲的路径仍503。
- `--min-available-memory-mib`默认2048，0显式禁用；
  `--memory-recovery-margin-mib`默认512、必须正数。工程缺省值沿用
  既有MTP预检2GiB余量，并增加512MiB恢复滞回，不声称适合所有负载。
  两项最大1048576MiB；`--no-budget`只影响启动容量估计，不禁用此门。
- 初始及恢复要求可用量达到floor+margin；已开放时低于floor关闭。
  读数失败立即关闭，不使用上次健康值；普通查询共用最多250ms样本，
  请求准备/grammar前和取得slot时强制新读。排队者100ms轮询退出。
- chat/preflight在body预留前、准备前检查；生成取得slot前再次检查。
  503固定code为`memory_pressure`或`memory_observation_unavailable`；
  已发SSE头时沿用单error+[DONE]，没有成功finish或第二个HTTP响应。
- 正在生成的请求继续完成或响应原取消/截止；不强杀、不回收他人进程。
  readyz和q4t_ready考虑内存门；livez/healthz/取消不因压力关闭。
  config/capabilities声明边界，固定4状态和3检查点×2原因指标；日志
  只记trace/checkpoint/reason，不新增请求内容或高基数指标。

Linux的MemAvailable是可用量**估计**，并不保留内存；并发申请、启动加载、
已经进入的生成以及驱动计量仍可能耗尽RAM。依据
[Linux proc文档](https://docs.kernel.org/filesystems/proc.html)。
`memory.high`与`memory.max`有不同的回收/限额语义，且不能据此推导
Thor CUDA分配已被完整计入memcg，见
[cgroup v2文档](https://docs.kernel.org/admin-guide/cgroup-v2.html)。
本机缺少`/proc/pressure/memory`，本阶段不依赖PSI，也不改共享会话cgroup。
完整物理RAM保护、cgroup/驱动归属、持续混合负载仍为独立待办。

## 成组验证与停止条件

先完成这组实现、机器合同和针对性测试，再统一CPU验证：严格观测解析、
未知MTP预检、精确阈值/滞回、失效读数、250ms缓存、并发一致、配置禁用/
事务边界；两种grammar构建及必需合同。原生未加载HTTP验证在GPU锁内：
无body情况下提前拒绝且额度0、control/ready/live、取消、恢复与单SSE
错误；队列压力/失效退出及回收。注入读数只证明机制，不称真实内存耗尽。

所有输出置`.q4t-work/serve-memory-20261103`，首次失败保留，仅重验受影响项。
CPU通过后再冻结真实模型HTTP的独立输入、预算与压力手段，不能为了触发
阈值把本机推至OOM。实际业务需覆盖普通/strict/真实工具回填与回收；尚未
运行、尚未冻结真实模型预算时不声称交付，不替换默认程序。

## CPU结果与真实HTTP冻结

候选84d16983，生产fault宏OFF，零警告；OFF224/ON233、必需190、后端9，
锁内未加载HTTP/队列19项通过。配置schema15+7项和驱动7项通过。
原inventory漂移失败保留；复核38字段/10路由、全部变化锚点，补上runtime
memory读数源锚点后定向通过。其余已通过项不重复。

原生19项的stdout/stderr只保留在本聊天工具输出，未重定向原始文件；
封存guard COMPLETED/child0、同ELF身份、注册表和派生receipt，明确不是
完整原始日志，不为补日志重跑已通过项。实际模型阶段单独留完整日志/wire。

真实HTTP计划使用同一生产ELF，loopback18105、完整head、MTP k3、单8192
槽、prefill2048/inflight8/queued2。计划5生成/4拒绝/2启动，加1次正常预检；
硬上限6生成/6拒绝/3启动，首次取得阶段锁至finalize1800秒，首次失败停下
保留，不自动补样/重启。原生19项直接复用，不再执行。

1. 第一实例显式floor1048576MiB：用真实/proc读数触发拒绝，不施加内存
   耗尽。2个chat与1个preflight只发16MiB声明的头、不发body，必须提前
   503/memory_pressure且body/chat/worker为0；此时不知道body的stream
   参数，不能宣称这两次chat分别证明JSON和SSE。ready503、live/health200，
   未知取消404而非内存拒绝，3次拒绝日志与固定指标精确对账。
2. 第二实例使用默认floor2048/margin512：实际预检、普通JSON、strict
   JSON/SSE，以及固定JS SDK一次真实MODEL.md工具读取和引用回填（2生成）。
   检查wire封套、任务质量、全部资源归零和worker回收、观察状态与配置一致。

不同实例之间的高/低门限不是同一模型进程压力恢复证明；同进程动态恢复
仅有原生未加载HTTP/队列的注入读数证据。本阶段不测OOM、强制cgroup限额、
驱动物理归属、持续混合负载，不声称性能非劣或全机保护闭合。

满足以上限定合同才按已授权收尾：保留上一默认pair与回退链、替换生产
候选/sidecar，安装CLI/隔离worker检查，并以同SHA候选真实HTTP复用作为
HTTP健康证明，不另起安装模型。失败保留并按已知身份回退。长期目标active。
