# 原生服务与旧托管 host 资源观测

当前Guardian服务使用项目原生观察入口，无nginx/NVML/Prometheus要求：

```bash
build/service/bin/q4t service observe --state build/service-native-state
build/service/bin/q4t service metrics --state build/service-native-state
build/service/bin/q4t service export --state build/service-native-state --port 19448
```

新入口随v6包发行；当前实现/验收以[STATUS](../../docs/STATUS.md)为准。
export前台监听127.0.0.1，提供只读GET/HEAD `/metrics`、`/status`；
不启动服务或发生成/探测/重启请求。采集器独立进程，单worker最多
3次/60秒启动，未回收保留容量；采集4秒无回复终止该采集器。HTTP
16连接、8KiB头、128KiB响应、2秒连接期限，内存快照超过3秒即UNKNOWN。
公开状态不包含PID、路径、凭据或请求内容；固定标签，不无限增加代次
标签。服务可接单与辅助观察UNKNOWN分别表示；资源缺失省略数值。
/proc RSS/Threads仅该进程记账，不是Thor总RAM或硬资源限额。
observe/metrics是显式一次只读采集，export独占该state的观察器锁；
只有export自动有界恢复自己的采集器，不能操作模型或Guardian。

以下为保留的旧backend v5/v6与可选nginx exporter入口，不是当前原生
服务的默认部署依赖。

只读绑定一个 v5 后端包及一次 run，读取受管 cgroup 的 host 内存、
任务计数和保护事件，并为 Prometheus 提供独立的 loopback `/metrics`。
不启动、停止或重试目标模型。memcg 读数不等于 Thor 的全部物理 RAM，
不能与 CUDA requested bytes 相加来声称总量。

当前验收状态以 [STATUS](../../docs/STATUS.md) 为准，范围见
[SRV-12f](../../docs/SERVE_HOST_OBSERVATION_2026-11-04.md)。
[本机真实采集、过期恢复、终态及工具短验已限定接受](../../docs/SERVE_HOST_OBSERVATION_RESULT_2026-11-04.md)，
原夹具与验收行序误判保留；未部署正式通知渠道或执行长稳。

## 准备与显式运行

所有目录须在本仓库 `build/` 或 `.q4t-work/`，由当前用户私有持有。
使用已验证的 backend v5 包和 nginx，保存返回的 policy SHA；示例中
的大写摘要需要替换为真实值，不能直接执行。

```bash
python3 -B tools/serve_host/observer.py prepare \
  --backend-package .q4t-work/backend-package \
  --backend-manifest-sha256 BACKEND_MANIFEST_SHA256 \
  --backend-run .q4t-work/managed-run-001 \
  --deployment thor-internal \
  --nginx .q4t-work/managed-gateway/nginx \
  --port 19448 --output .q4t-work/host-observer-001

python3 -B tools/serve_host/observer.py watch \
  --state .q4t-work/host-observer-001 --policy-sha256 POLICY_SHA256
```

prepare 固定源码、解释器、nginx 和配置，检查 nginx 语法，不启动
监听或模型。新准备包为v2，补齐网关TLS模块的完整依赖；旧v1包使用
各自固定源码入口，不能把当前源码复制覆盖到旧包。run 路径可尚不存在；首次看到的 controller/目录身份
会被锁定，之后替换不能自动重新绑定。新后端代次须显式准备新的观察
包及新状态目录；先保留旧代次证据，不在原路径重建服务。

watch 前台运行，独占 observer.lock，私有文件原子更新。nginx 只允许
`127.0.0.1:PORT/metrics` 的 GET/HEAD；不接受查询参数，不提供其他
业务或管理路由。只能由同机可信 Prometheus 采集，不能转发到业务
TLS 网关。每秒读取源状态，源失联仍持续输出 UNKNOWN；自身 nginx
或日志错误会清理自有子进程并退出，不会改变目标模型。

用同一入口的 `status` 查看已有快照及年龄，`metrics` 输出已有文本；
读取不会刷新采样时间。`observe` 是无监听的一次采样，运行中的
watch 持锁时会拒绝它。`unit` 输出 Restart=no、无 Install 的模板，
不会自动安装/enable。观察器停止用它的前台终端 Ctrl-C，或对已核对
身份的自有观察器发送 SIGTERM；它负责回收自己的指标 nginx。
确认 `observer-terminal.json` 的终态及 cleanup_confirmed，不能用
观察器终态替代模型/GPU 的实时清理证明。

## 指标与判断

所有 `q4t_host_*` 公共指标只用固定标签；deployment 是受限名称，
generation 是观察包 policy 的 SHA，绑定后端包和唯一 run 路径。
PID、cgroup 路径、凭据和请求内容只留在私有记录中。

| 指标 | 含义 |
|---|---|
| state / reason | 生命周期单选与固定原因；RUNNING 不等于 API ready |
| evaluated_timestamp_seconds | 观察器采样时间，旧静态文件仍保留原时间 |
| snapshot_kind | current 为当下内核，final 为结束证据，none 为无数值 |
| resource_observation_valid | 在该 kind 下是否具有数值证据；final 有效仍是历史 |
| source_age_seconds / backend_ready | 新鲜 worker 的年龄及 ready；缺证时省略 |
| cleanup_confirmed | 历史 controller 的清理记录；不是新的 GPU 查询 |
| memory_current_bytes / memory_peak_bytes / swap_current_bytes | host 记账域的当前、峰值及 swap |
| tasks_current / tasks_max | 内核 pids 计数包含线程，及该域上限 |
| memory_high_bytes / memory_max_bytes | 已核对的 host 限额 |
| memory_events_total / tasks_limit_events_total | 单代次事件累计；结束后保留历史值 |

缺数值时省略，不能填成零。目录、controller、资源记录、boot/namespace
或进程身份不符，状态过期或读取失败均 UNKNOWN。worker 单调时钟年龄
加墙钟检查防止 suspend 延续旧 READY；时钟偏差也可能暂时报 UNKNOWN。
该工具信任同 UID 的私有源，不能抵御管理员伪造，也不是独立物理传感器。

## Prometheus 与告警处置

使用 [serve_notify](../serve_notify/README.md) 的显式 v2 policy，增加
`"host_observer_port": 19448`；其他字段保留。旧 v1 不增加 target。
配置会载入 [10 条 host 规则](../../deploy/serve-ops/q4t-host.rules.yml)，
保留 generation 标签并在 Alertmanager 中按 generation 分组。
host scrape 为 15 秒、规则为 5 秒；30 秒过期门限并不等于最坏告警
交付时延，还受采集、评估、分组与通知链路影响。

| 告警 | 首先核对 |
|---|---|
| Q4THostExporterDown / TargetMissing | 目标端口与采集配置；absent 只检查是否至少有一个 host target，不能代替多台部署清单 |
| Q4THostMetricContractMissing | 七种 state 的数量和单选和；不是全部指标/schema 的完备校验 |
| Q4THostObserverStale | 观察器是否停顿、静态文件是否仍更新，以及时钟偏差 |
| Q4THostSourceUnknown | 私有 status 的固定原因，实际 run/权限/身份和 worker 新鲜度；不要盲目信号旧 PID |
| Q4THostGenerationWaiting | 预期的代次是否已显式启动，超过两分钟仍未进入运行状态 |
| Q4THostGenerationTerminal | 计划停机还是失败；final 数字是历史，不代表有模型在运行 |
| Q4THostProtectionFailed | 该代次的 OOM、pids 拒绝或保护丢失；只调查并明确换代，不自动重试请求/工具 |
| Q4THostThrottlingObserved | high 事件累计，即使压力已解除也保留；单独 memory.max 事件不等于 OOM |
| Q4THostHeadroomLow | 仅 current 快照距限额不足 10% 持续 30 秒；阈值只用于告警，不扩大保护合同 |

缺少正式接收器、LAN/CA/DNS 配置时不得声称远端通知或上线已验。
监控和模型共用设备，整机宕机仍需独立缺失检测与外部接收方；
本批只进行有界短验，用户负责小时/天级长稳。

封存后的离线核验不加载模型或访问 HTTP：

```bash
python3 -B tools/serve_host/projection_fix.py verify
```


## 当前原生服务run留存（v6）

`service retention-plan --state STATE --plan-output PLAN`生成只读计划，
可设置`--max-age-days`、`--max-runs`、`--max-bytes`。只计算runs中文件
实际分配块；未知字节不会冒充0，受保护对象超额单独报告。state、
应用对象、模型目录、发行/回退包不是清理候选，归档数量不在该限额中。

`service retention-archive --state STATE --run UUID --archive DIRECTORY`
先复制到state之外的私有work/build目录，完整字节和身份核验后登记。
未归档现场、活动引用、发行/回退依赖保留。`service retention-apply
--state STATE --plan PLAN`复核原计划后执行；异常中断用`service
retention-resume --state STATE --run UUID`，只删除原清单的未变残留。
归档及删除均不会停止模型或获取核心持久锁。

旧格式或跨boot身份不明确时，先在服务已持久STOPPED且无活动代次
时执行`service retention-migrate --state STATE --run UUID`。它不主动
停止服务；非阻塞核对三把拥有权锁，在锁内不做文件I/O，前后核对
账本后写外部证明，保留历史配置。任一锁忙、账本变动或归档损坏
均拒绝；新启动仍必须使用新UUID。清理只在显式命令下执行。
