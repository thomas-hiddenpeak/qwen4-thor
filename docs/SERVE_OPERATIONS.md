# 本机 serve 运维手册

适用当前单台 Thor、受控本地服务与 OpenAI 兼容文本接口。工具只读取
本机控制入口，不自动生成、取消、重试或重启。验收范围见
[SRV-12 合同](SERVE_OPERATIONS_2026-11-03.md)，当前交付身份以
[STATUS](STATUS.md)及本机发布 sidecar 为准。

## 收集诊断

先确认自己负责的服务 PID 与端口，记录业务是否仍在输出。不要用同名
进程匹配结果直接 kill；PID、启动 ticks、boot ID、监听 socket 归属与
ELF SHA 必须一致。开发验证仍遵守 GPU 独占约定，诊断工具不会获取或
转移业务服务的管理权。

```bash
install -d -m 700 .q4t-work/operator-bundles
python3 -B tools/serve_ops/diagnose.py \
  --base-url http://127.0.0.1:8080 --pid 12345 \
  --release build/q4t.release.json --log .q4t-work/owned-server.log \
  --output .q4t-work/operator-bundles/first
python3 -B tools/serve_ops/diagnose.py \
  --base-url http://127.0.0.1:8080 --pid 12345 \
  --previous .q4t-work/operator-bundles/first \
  --output .q4t-work/operator-bundles/second
```

示例 PID、端口与日志路径需替换为实际拥有的服务。诊断依次 GET
`/livez`、`/readyz`、`/v1/capabilities`、`/v1/config`、`/metrics`，每次
截止 2 秒、全组 HTTP 10 秒；单头 16KiB、单体 256KiB。只接受数字
loopback 地址和显式端口，拒绝代理、重定向、分块或不完整响应。
同机 IPv4 socket 归属、网络命名空间和前后进程身份必须稳定。
发布元数据与 ELF、两个协议指纹共同核对；capabilities 本身不是发布
证明。该工具不支持远端代理、容器跨网络命名空间或 IPv6 监听者鉴别。
诊断先读取 capabilities 的 observability 版本：v3 的 config/capabilities/
metrics 使用普通连接额度；v4 将四个精确 GET 观测路由（再含 models）
独立预留4个连接，health/live/ready/取消仍共享另一组4个控制连接。
v4开发与验收进度见[容量合同](SERVE_OBSERVATION_CAPACITY_2026-11-03.md)，
默认交付版本以STATUS为准。观测满为503/observation_capacity_exhausted，
报告UNKNOWN；未知慢头仍共用处理线程总额，不能保证洪泛下可用。

退出码：0 HEALTHY、1 WARNING、2 CRITICAL、3 UNKNOWN；4 表示
参数/本地包创建失败。UNKNOWN 优先表示证据不足，报告仍保存已知
critical finding；不能因为缺指标、失联或身份不符而报告健康。无上份
包时只有当前快照，不保证过去无故障。仅同一已验证进程、相同协议、
间隔 0～3600 秒、计数未减少才输出逐项 delta。重启、坏包、counter
reset 都输出 unavailable，不能补零、跨重启拼接或加总重叠事件。

快照不是原子操作；有界日志尾部与抓取间隔可能错过短事件。诊断报告
不能证明物理内存峰值、完整任务成功率或长期无泄漏。

## 配置监控

[Prometheus 示例](../deploy/serve-ops/prometheus.example.yml)与
[v4规则文件](../deploy/serve-ops/q4t.rules.yml)提供 17 条告警、2 条记录规则；
旧v3二进制和回退服务使用[v3规则](../deploy/serve-ops/q4t-v3.rules.yml)。
通过capabilities明确选版本，不可把v4规则套在v3服务上。
`tools/serve_ops/rules.py --check` 核对生成结果。必须用实际部署版本的
`promtool check config`、`check rules` 与 `test rules` 验证；本阶段工具
固定 3.5.0，不表示最新版本推荐。

job 固定为 `q4t`，instance 分开匹配；默认 scrape/evaluation 15 秒、
timeout 2 秒。只抓取已拥有的 loopback 端口。模板没有通知接收器；
真正上线还需配置接收、路由、抑制、值班与断链演练。监控进程自身
停机无法依赖其自身规则通知，需要独立外部存活检测。

`TargetMissing` 发现整个 job 没有 series；已有部分实例时，单个预期
实例从服务发现中消失，需要外部预期实例清单。`MetricContractMissing`
按版本固定家族的87（v3）或92（v4）条series数检查完整性；同数量的错误标签替代须由
诊断工具固定标签合同发现。Prometheus 使用浮点时序，不能代替诊断
工具对大整数的精确投影。规则持续时间是本地调查门槛，不是用户 SLO。

### unavailable

`Q4TTargetDown` 连续 1 分钟抓取失败，`Q4TTargetMissing` 连续 2 分钟
job 缺失；先分清监控配置、端口、服务退出与系统故障。不要直接将
`up=0` 解释为 GPU 错误。v3抓取可能因普通连接额度耗尽，v4可能因观测额度或未分类总额耗尽
而失败；这些都不能直接推断进程退出。`Q4TNotReady` 在 GPU 健康且内存门开放时
持续 2 分钟未 ready，检查启动/排空状态。`/livez` 活着不表示可接单；
正常忙和槽被占用不应改变 ready。先收集诊断与 owned service 状态，
再依既定服务恢复合同处理，不盲目重启或抢 GPU 锁。

### observation

`Q4TMetricContractMissing`、`Q4TMetricContractInvalid` 持续 30 秒：
检查抓取是否完整、版本/协议/标签是否匹配、布尔值和内存 one-hot
是否有效。NaN、缺失、身份变化都不能当零。CLI 的
`identity_unverified`、`endpoint_observation_failed`、
`log_projection_unavailable` 同样表示 UNKNOWN；保留错误码，不替换
为其他实验的观测。监控失效时不能据此接受 GPU/业务验收。

### gpu

`Q4TGpuUnhealthy` 立即提示已观测的 sticky GPU 错误。停止把新工作
交给此实例，保存日志/身份/驱动状态；按拥有者的恢复流程处理。诊断
工具不执行恢复。不能仅凭 `/livez=200` 覆盖 GPU unhealthy。

默认程序的`[q4t][cleanup-fatal] api=… phase=… error=…`表示直接CUDA
释放/完成失败，进程立即退出1，可能来不及发布GPU指标或HTTP错误。
这时抓取失败与日志/退出身份合并判断，不要求先看见unhealthy指标。
保留已收到的响应与未完成工具状态，不把断流当成功或自动重放未知
工具结果。serve已处理的设备释放错误仍走stop/drain及可写时的错误
封套；底层无法确认排空时仍立即退出。见[02g范围](SERVE_BACKEND_CLEANUP_RESULT_2026-11-03.md)。
请求MTP加载失败现会阻止任一启动路径；需要plain应显式配置`--no-mtp`，
不依赖静默降级。其他CUDA操作/外部库与硬件恢复不由本批证明。

### memory

`Q4TMemoryObservationUnavailable`、`Q4TMemoryPressure` 持续 30 秒：
前者是观测失败，后者是实际低可用内存；新工作拒绝与既有工作输出
分别记录。核对有效 floor/recovery、系统可用内存与同机服务；不要
手动清页缓存或杀他人进程。已验证的滞回恢复不需要重启模型。
`Q4TMemoryGateDisabled` 持续 5 分钟只提示显式禁用，需确认部署意图。
当前运行期门限是提前准入；启动有独立的三次可用内存检查，
两者**都不是物理RAM预留或硬限额**。Thor
host/device 同用 DRAM，RSS、NVML、memcg 存在重叠/漏计，不相加估算
总容量；worker lifetime maxrss 含 pre-exec，不能当 post-exec 峰值。

### capacity

`Q4TAdmissionRejections` 持续 1 分钟检测 5 分钟窗口内准入拒绝增加。
`capacity_exhausted`、`queue_capacity_exhausted`、
`structured_capacity_exhausted`、`body_capacity_exhausted` 分属不同
资源，先看 active/queue/body/worker 与有效配置。内存拒绝还带
headers/prepare/execute checkpoint。不要将 receive、chat、memory
多个视图相加当失败请求总数；先降低上游并发/请求体或排队预算。
请求确认尚未执行且业务允许时，客户端才可显式有界退避，工具默认
零自动重试。提高阈值前重新评估 RAM，不能只看空闲槽。

### structured

`Q4TStructuredFailures` 立即提示 5 分钟内准备失败/编译截止/资源
耗尽增加。检查 `constraint_compile_timeout`、
`constraint_resource_exhausted`、`constraint_worker_failed`，以及
schema 大小、支持子集、worker 回收。预检只证明当前输入可接受，
不预留执行资源。`structured_output_incomplete` 或 invalid 表示不能
交付完整严格结果，不能将部分 JSON 当成功。普通路径默认 MTP，
严格输出当前走 plain constrained，不混淆两者成本或能力。

### errors

`Q4TChatErrorsObserved` 为 information：5 分钟内 chat error 增加
可能只是输入拒绝，不是模型错误率。先查固定 code 与阶段；400 修正
输入，404 确认路由/模型/任务身份，413 缩小体积，415 修正媒体类型，
503 区分资源/内存/排空，500 检查 worker/解析/GPU 或内部异常。
请求 deadline、主动取消、客户端断开与业务 error 分列。嵌套 schema
部分错误仍 `param:null`，不能捏造精确字段位置。

工具响应必须以完整终态和全部 tool_call ID 对账后执行，并将真实
结果按原 ID 回填。流中断/超时后的工具结果属于未知状态；禁止自动
重放有副作用工具，先核对应用任务库与已持久化结果。会话 KV 不是
任务数据库，通用跨进程恢复仍未交付。现有只读项目工具也不能把
部分参数/部分答案标为完成。

### transport

`Q4TTransportWriteFailures` 覆盖 write_deadline/socket_error/exception；
`Q4TTransportReceiveFailures` 覆盖 header/body deadline/socket_error/
exception。主动取消、正常 shutdown/peer close 不作为这两条告警。
输出成功计量只表示本地写入，不能证明客户端已接收或已执行任务。
用匿名 trace/completion 关联第一次传输失败；handler 与 heartbeat
记录分列，不能与请求 outcome 合并成可相加的失败总数。检查代理
缓冲/超时与慢客户端；当前验收只覆盖本机 loopback。

### owners

`Q4TWorkerReapStalled`、`Q4TIdleResourcesRetained` 在已观察到业务
空闲后持续 1 分钟提示 worker/body/strict/slot 未释放。活动工作拥有
资源属于正常，不自动报泄漏。v3 `/metrics`自身计入business_connections，
空闲条件为active_chats=0且business_connections≤1；v4自身属于观测
连接，空闲要求business_connections=0。尚在接收body的业务连接不
满足v4空闲条件；不得混用版本。核对多次快照、子进程身份和截止；
单一瞬时读数、worker started-reaped 或退出中无 VmRSS 不等于物理
泄漏证据。禁止因这些调查告警直接 kill 未确认身份的 worker。

### progress

`Q4TExecutionProgressStalled` 在有活动工作且槽被占用时，5 分钟窗口
无 execution boundary 变化，再持续 5 分钟触发。它提示调查长
prefill、GPU、锁或写阻塞，不证明死锁，不自动取消/重启。
两条 p95 记录分别来自本地 response 与 slot wait histogram，只
覆盖已记录样本；无样本可以为 NaN。没有用户端端到端延迟 SLO，
不把这两个分位数相加或用于跨实例分位数平均。

## 日志与诊断包留存

原始服务日志可能含模型路径、用户提供的 request/trace ID 与异常
信息；本轮没有改写所有原始日志。仅授权运维访问，限制原文件权限，
不要把原日志作为已脱敏诊断包共享。诊断导出只保留固定枚举/数字，
ID 用每包随机 HMAC 关联并丢弃密钥；不含 prompt、工具结果、token、
原始 HTTP 头/体、模型名/路径。未知字段/行不原样导出；每包最多
2MiB 日志尾部、512 事件，跨包无法关联。这里的白名单不是对恶意
同机伪造日志的真实性证明，hash 自校验也不是签名认证。

[service drop-in 模板](../deploy/serve-ops/q4t-logging.conf.example)使用
独立 `LogNamespace=q4t`；配套
[journald namespace 模板](../deploy/serve-ops/journald-q4t.conf.example)
只用于 `/etc/systemd/journald@q4t.conf`，不能覆盖全局日志配置。
配置持久 256MiB、运行态 64MiB、14 天，禁用日志速率丢弃；仍需监测
磁盘与写错误。journald 限额/留存作用于其管理的日志文件，不是严格
即时磁盘上界，活跃文件可能延后清理；不能据此声称原始日志零丢失。
部署前确认本机 systemd 支持 namespace、专用日志权限与值班访问。
模板尚未安装，不为当前已有文件日志自动提供轮转或容量保证。
namespace 路径及保留策略依据
[systemd 255 journald 配置](https://www.freedesktop.org/software/systemd/man/255/journald.conf.html)，
服务选项依据
[systemd 255 执行配置](https://www.freedesktop.org/software/systemd/man/255/systemd.exec.html)。

```bash
python3 -B tools/serve_ops/retention.py \
  --directory .q4t-work/operator-bundles
# 审阅计划后，显式删除超过 14天/100份/100MiB 的本用户私有诊断包
python3 -B tools/serve_ops/retention.py \
  --directory .q4t-work/operator-bundles --apply
```

默认 dry-run；apply 重核身份/完整性，只删除该目录的标准两文件包。
其他文件、符号链接、损坏或并发变化拒绝/忽略，不递归清理证据目录。
留存额度仅统计已识别包，不保证整个父目录容量；本用户并发修改仍
不是被隔离的攻击边界。证据归档必须独立封存，不放进自动清理目录。
