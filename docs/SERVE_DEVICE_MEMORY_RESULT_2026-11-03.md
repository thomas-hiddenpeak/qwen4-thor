# SRV-02f 设备资源失败与释放结果

限定接受并交付。默认ELF `40cf6376`、sidecar `79d4a657`、protocol
`0075631e`，203运行时来源；实现冻结41cceacb，取证纠正c8b656b9。
完整head、MTP k3。模板、分词、grammar与模型数值算子未变，旧证据
按身份复用；改变的是serve所有权、错误传播及成功终态前清理顺序，
不作性能非劣或完整模型质量声明。

[原冻结合同](SERVE_DEVICE_MEMORY_2026-11-03.md)、
[失败与定向纠正合同](SERVE_DEVICE_MEMORY_CORRECTION_2026-11-03.md)、
[原始证据索引](evidence/serve-device-memory-20261103/manifest.json)。
工业化目标继续active；本项只覆盖文本请求与调度器直接拥有的设备缓冲。

## 交付行为

- 明确OOM且没有其他pending错误才允许可选缓冲降级；必需请求缓冲
  返回`device_memory_exhausted`，JSON503或HTTP200后的SSE错误。
- 其他分配错误、任何释放错误关闭ready并停服，逻辑错误为
  `gpu_unhealthy`，进程非零退出。请求侧先在model锁内排空，再展开
  剩余清理；排空失败立即退出，不复用未知设备状态。
- 释放所有权只消费一次；未知结果不重试原指针。成功终态与缓存发布
  之前检查设备缓冲回收。晚期错误允许已发送部分内容，但没有正常
  finish/usage，客户端不得把部分输出当完整答案或工具调用。
- 未分类MTP准备Status不再盲目降级；调度缓冲析构失败不再退出0。
  两条已有媒体早退改由外层RAII解锁后清理，避免新排空路径递归锁；
  媒体完整错误处理仍未接受。
- capabilities公开限定范围。故障钩子仅在独立validation构建；
  生产编译关闭并核对编译命令。没有真实耗尽物理RAM或破坏GPU。

## 实际验收

CPU OFF241/ON250、必需207、后端9、原驱动6和取证纠正4、配置15+10
通过，生产/validation零警告。排空补强后协议身份使部分测试ELF变化，
原复用拒绝保持；最终必需/配置及防漂移门定向重验，未变语义证据复用。
锁内原生HTTP生命周期25项通过，包含fatal状态在Run之前已设置时
不能返回成功或发布listener。38字段/10路由/嵌套白名单没有扩张。

| 场景 | 真实结果 | 限定解释 |
|---|---|---|
| 生产普通JSON/SSE、strict JSON、JS资料工具及答案 | 5成功；实际读取MODEL第1–24行、事实及引用正确；worker3/3reap | 同一生产ELF，非validation伪造输出 |
| 可选prompt缓冲OOM | 降级成功，同PID下一轮MTP成功；共2成功 | 注入API状态，不是实际内存耗尽 |
| 必需session缓冲OOM | 完整SSE错误、owner归零；同PID继续真实seed/restore/fresh | 实际命中8473 cached tokens，缓存与fresh答案一致；不宣称数值等价 |
| 非容量分配、早期prompt释放、晚期draft释放、MTP准备失败 | 各返回gpu_unhealthy、exit1；晚期已发内容后只有错误终态 | 未注入真实异步GPU损坏，不能保证真实不可写连接必有完整错误 |
| 启动scheduler第二缓冲失败 | exit1，无ready/listener | 模型已加载，不能当作模型加载前拒绝 |
| 正常请求后shutdown prefill释放失败 | 请求成功保留，退出码1 | 不把后续析构失败追写成已经完成请求的错误 |

首轮必需session OOM在标准客户端收到error事件后立即抛异常，sink
未读DONE。正确错误码与资源日志存在，但完整尾部缺证，因此原
[FAILED](evidence/serve-device-memory-20261103/original-result.json)与
[COMMAND_FAILED](evidence/serve-device-memory-20261103/original-guard.json)
保持。原失败模型干净退出，GPU释放，默认当时没有替换。

纠正先独立读取到EOF，再验证同一wire的错误终态与客户端拒绝提交。
只重验该失败场景并完成未执行项；原生、生产5轮与可选OOM2轮不重跑。
原16/7/9次数预算未改写，新的独立补验预算9/7/7；累计17次生成尝试、
8负例、10启动，明确比原计划多一次故障请求和一次必要重启。
原失败不重分类为通过；两段租约分列，各段自身持完整锁。

累计11次成功、5份完整错误wire及1份原始错误尾部缺证；另外两项
进程负例为startup/shutdown。16份完整wire共47封套校验，原不完整
wire另留。业务控制/发现请求61，762.663秒finalize，原1800秒总时间
及1500秒安装前界保持。纠正guard COMPLETED、ENV_ABORT0；没有网络
自动重试或新增未冻结场景，没有性能接受声明。

328个独立时间样本保留：原111、纠正新217；复制复用的78行不再计数。
致命请求前停止live-PID采样，退出后由pidfd/scope/cgroup与NVML确认，
所以不称覆盖退出瞬间峰值。启动失败没有连续PID采样。各域不相加；
API成功/进程退出不等于已证明全机每一字节物理页立即归还。

## 安装、回退与证据

[交付完成](evidence/serve-device-memory-20261103/finalized.json)与
[安装健康](evidence/serve-device-memory-20261103/installed-health.json)：
默认二进制/sidecar匹配；实际CLI与隔离worker握手通过，HTTP明确复用
同SHA生产候选，没有另起安装路径模型。

`.q4t-work/serve-device-memory-correction-20261103/rollback-bundle/`
保存新40cf6376/79d4a657与直接旧f69982d5/34de1c8f、各cache和v4规则。
只读验证通过，本阶段没有实际切换回退；40个匹配pair历史链保留，
末端旧stale边界仍不可安装，不伪造第41个pair。

15:17:20 CST后检GPU空、共享锁可用、owned scopes移除、无恢复标记，
MemAvailable116.525GiB。839原始记录无损封存为19份Git文件约1.385MB，
20个ELF/object/source bundle本机身份保留；原始wire/log不称全档脱敏。
[保管核对](evidence/serve-device-memory-custody-20261103.json)另外记录
19份暂存Git blob、839解压原件、20本机产物与594变更文档链接检查
全部通过；封存后没有追加模型或HTTP测试。

## 下一阶段

按代码审计继续收紧默认文本路径：legacy MTP加载失败仍有未分类降级，
MTP/model内部释放缺少完整错误传播。媒体pipeline的裸分配/同步/
释放及错误HTTP分类随后补齐，不以本批文本检查替代媒体验收。
整机物理RAM保护、host耗尽时错误响应、实际入口/通知/多人策略、新ELF
小时/天级稳定与跨进程业务恢复继续留在工业化任务账；14历史失效文档
链接也未借本次封存自动关闭。
