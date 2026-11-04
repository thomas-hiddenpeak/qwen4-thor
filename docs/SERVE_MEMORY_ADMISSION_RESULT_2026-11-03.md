# 统一内存准入：限定交付

2026-11-03，**ACCEPT_BOUNDED_MEMORY_ADMISSION**。新增全局可用内存准入、
观测失败拒绝、恢复滞回、就绪状态和固定指标；修复MTP预检读数失败仍
尝试申请可选大缓冲的问题。生产程序已安装，GPU已释放；不是整机RAM
硬限额、OOM防护完成或整个serve工业化完成。

| 身份 / 范围 | 记录 |
|---|---|
| 实现、驱动与冻结合同 | 326eff19；[合同](SERVE_MEMORY_ADMISSION_2026-11-03.md) |
| 当前默认ELF | 84d16983e03c21c62dd249f4c7e9785bb5e62a010e21947778b26b001d84d94d |
| 当前sidecar | c520333192c04ddb499dd0188496789ec8ddc20fc5e0f00491ca01f52517aa75 |
| 协议SHA | aeb66adc439c31b23d943ffe9d9f40253fe2df275f89767b87927ab979bd4bd5 |
| 原默认回退 | ed672a65 / 67897c81；封存完整pair及既有回退链 |
| 生产配置 | 完整head、MTP k3、单8192槽、prefill2048/inflight8/queued2；loopback18105 |
| 真实计划 / 实际 | 5生成、4拒绝、2启动，加1正常预检；首轮完成，无重试 |
| 硬限 / 时间 | 6生成、6拒绝、3启动、1800秒；业务81.070秒，首次锁至finalize106.751秒 |
| 独占与收尾 | guard COMPLETED、ENV_ABORT0；已安装、CLI/worker健康通过 |

198个运行时文件登记身份，模型/数值、模板、分词、grammar/parser未变；
内存准入改变是否接纳请求，MTP观测未知时改变可选缓冲申请/回退策略。
生产fault宏OFF，没有新的运行时观测注入开关。性能非劣未评价。

## 实现与直接验证

16KiB有界`/proc/meminfo`解析，严格检查字段、kB单位、正负/溢出/重复/
截断；有效0与未知分开。`--min-available-memory-mib`默认2048，0禁用；
`--memory-recovery-margin-mib`默认512且必须正数。恢复/初始开放需达到
floor+margin，已开放后低于floor关闭，失败读数不能沿用旧健康状态。
普通观察最多缓存250ms，准备/grammar和slot交接强制刷新；排队者轮询退出。

chat/preflight在body预留前、准备前检查；生成取得槽位前复查。错误为
503/memory_pressure或memory_observation_unavailable；首帧后沿用单SSE
error+[DONE]，无成功finish或第二HTTP响应。进行中的生成不被内存门强杀。
readyz/q4t_ready同步关闭，livez/healthz/取消仍受原控制连接合同保护。

CPU OFF224/ON233、必需190、后端9，配置schema15+7和驱动7通过，零警告。
锁内未加载生产HTTP/队列19项通过，覆盖无body提前拒绝、读数失效、
精确恢复阈值、控制取消、一次SSE错误、队列注销和槽位回收。原inventory
漂移拒绝保留；复核38字段/10路由及变化代码，扩runtime读数锚点后定向通过。

**证据限制：**19原生检查stdout/stderr仅在聊天工具输出，未保存为原始
日志文件。归档包含guard COMPLETED/child0、同ELF身份、注册表和派生
receipt；不把receipt当原始日志，不为补日志重跑已通过项。实际模型阶段
另行保留了完整服务日志和请求/响应字节。

## 真实模型服务验收

高门限实例用真实Linux读数、floor1048576MiB安全触发拒绝，没有额外申请
内存来制造OOM。两个chat和一个preflight只发送声明16MiB的HTTP头，
没有发送body；三次约0.21～0.24ms即503/memory_pressure，body/chat/
worker均0，固定拒绝计数、接收admission计数和三条trace日志一致。
ready503、live/health200；未知取消404/request_not_found，不被内存门挡住。
这些body未读的chat请求没有JSON/SSE模式区别，不拿标签推导流模式验收。

默认门限实例的1次预检与5次生成完成：普通JSON精确回复710003，strict
JSON和SSE均为`{"ok":true}`；固定Node24.18.0/openai7.47.0真实读取
docs/MODEL.md第1～24行，核对工具名/参数/ID后回填，回答准确包含
Qwen4ExpForConditionalGeneration及工具来源引用。客户端零自动重试。

普通约0.457秒、strict JSON/SSE约0.623/0.624秒，工具请求/最终回答约
3.871/4.168秒；是本次有界业务时延记录，不是对照性能接受。5份生成wire
和3份提前拒绝wire共17封套通过schema，取消、就绪、配置和预检另行校验。

正常实例成功5、错误0、中断0、内存拒绝0，worker4/4reap；最终body/
active/queue/strict/preparing/vocab/worker均0、槽1/1空闲。两实例正常退出，
启动预留1606/1606释放，无遗留模型/worker或恢复标记。

安装后CLI和隔离worker握手通过；HTTP健康复用**同SHA生产候选**的真实
ordinary/project-read/project-answer，不声称从安装路径另起了模型。
原pair位于`.q4t-work/serve-memory-20261103/package/previous`，封存发布绑定
可供`tools/serve_memory/stage.py rollback --output <新的独占lease目录>`
在`gpu_exclusive.py`内、使用同一output目录按既有独占入口回退；回退
检查不要求当前开发源码保持冻结，未实际执行回退。

## 内存观察和剩余事项

5个操作间稀疏样本：parent RSS最大943.996MiB，系统MemAvailable最低
33.888GiB；未采到活跃worker RSS，不能记作0。4份worker退出记录中
post-exec VmHWM最大224.059MiB，wait4生命周期最大3903.812MiB含exec前
继承峰值；不同口径不能相加。不是完整进程或整机物理内存峰值。

09:11:09 CST，GPU计算PID空、共享锁可用，无恢复标记，MemAvailable
116.988GiB；未drop_caches、未干预其他进程。归档
[195原记录、199文件、约2.09MB](evidence/serve-memory-20261103/README.md)，
7个ELF/o/bundle大产物本机身份登记，原失败和回退链保留。
199份Git暂存blob逐字节核对、7大产物/回退pair只读核验和574个本地链接
通过；证据整体显式入库，包含被常规ignore排除的.log/.bin。

本阶段只证明提前准入：MemAvailable是系统估计，不保留字节；启动加载、
已进入的生成及并发分配仍可能耗尽统一RAM。不同模型实例的高/低门限不
构成同模型进程动态压力恢复证据；该动态机制目前仅由原生未加载HTTP/
队列注入读数证明。后续继续物理RAM/驱动与cgroup归属、受载恢复及持续
混合负载；访问运维、长稳、通用任务恢复仍未完成。长期目标保持active。
