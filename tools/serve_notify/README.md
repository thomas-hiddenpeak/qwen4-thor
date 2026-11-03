# 告警通知接入

该工具把现有17条serve告警接入标准Prometheus→Alertmanager→HTTPS
webhook，增加恢复通知、链路心跳和通知故障指标。正式接收渠道尚待
配置，[本机接收器与真实恢复验证已通过](../../docs/SERVE_NOTIFY_RESULT_2026-11-04.md)；部署接受以[STATUS](../../docs/STATUS.md)
为准。接收器确认、人员看到通知、业务故障被处理是三个不同结果。

## 准备私有包

依赖固定Prometheus/promtool3.5.0及Alertmanager/amtool0.34.2 ARM64，
工具校验已审阅的SHA；不自动下载安装或启动后台服务。更新版本需
单独验证兼容性，不能仅替换磁盘上的二进制。

用0600文件准备Bearer随机凭据，policy也为0600；下面仅示意字段：

```json
{
  "version": 1,
  "deployment": "thor-internal",
  "runner_port": 18123,
  "prometheus_port": 19095,
  "alertmanager_port": 19096,
  "receiver_url": "https://ops.internal.example/q4t/alerts",
  "receiver_token_file": "/PRIVATE/PATH/receiver-token",
  "receiver_ca_file": "/PRIVATE/PATH/ca.pem"
}
```

接收器须支持Alertmanager webhook v4 JSON及Bearer验证；支持外部
CA时可省略receiver_ca_file，使用系统信任库。URL禁止user/password、
query和fragment，不跟随重定向，禁止关闭TLS验证。URL路径也可能是
私有信息；整包、原始组件日志和验证诊断都按私有运维数据管理。

```bash
python3 -B tools/serve_notify/notify.py prepare \
  --policy .q4t-work/notify-policy-private.json \
  --prometheus .q4t-work/serve-ops-20261103/deps/prometheus \
  --promtool .q4t-work/serve-ops-20261103/deps/promtool \
  --alertmanager .q4t-work/serve-notify-20261104/deps/alertmanager \
  --amtool .q4t-work/serve-notify-20261104/deps/amtool \
  --output .q4t-work/notify/package-001
```

prepare复制固定依赖/源码/私有输入并运行真实配置检查，不发送通知。
保存返回manifest SHA；以下MANIFEST_SHA256应替换为该完整值，Python
路径与manifest的python.path一致。准备一次独立持久状态：

```bash
/usr/bin/python3 -I -B \
  .q4t-work/notify/package-001/source/tools/serve_notify/notify.py init-state \
  --package .q4t-work/notify/package-001 \
  --manifest-sha256 MANIFEST_SHA256 \
  --output .q4t-work/notify/state-001
```

## 显式运行与停止

两个组件分别以前台进程运行，先alertmanager、再prometheus。每代次
使用新的output路径；将下例role及output换成prometheus后启动第二个
组件。运行会向policy明确配置的接收器发送通知，包括分钟心跳。

```bash
/usr/bin/python3 -I -B \
  .q4t-work/notify/package-001/source/tools/serve_notify/notify.py run \
  --package .q4t-work/notify/package-001 \
  --manifest-sha256 MANIFEST_SHA256 --role alertmanager \
  --state .q4t-work/notify/state-001 \
  --output .q4t-work/notify/alertmanager-run-001
```

将run换为unit、保持其他参数，会输出显式systemd模板；工具不会安装
或enable。模板Restart=no、KillMode=mixed、30秒停止窗，无Install/
timer。组件只监听127.0.0.1，Alertmanager HA端口禁用；同机控制面
属于可信边界，管理API不能转发到业务TLS网关。

用包内入口的`status --run RUN_DIRECTORY`查看，`stop --run RUN_DIRECTORY`
持久化停止意图并通知拥有者。STOP_REQUESTED不等于回收完成；等待
STOPPED/cleanup_confirmed=true，FAILED需同时查看error和清理字段。
OWNER_ABSENT保留未知终态，不按旧PID盲目清理。停止通知组件不会
停止模型或释放模型的GPU锁。

LISTENING只证明自有监听身份；还需本机/-/ready、Prometheus targets/
rules及接收端确认验证健康。运行中的模型失联仍继续监控并告警，不
跟随模型生命周期自动停监控。证书/Key更新需新私有包与明确换代，
当前不自动迁移绑定到旧包的持久状态。

## 接收与运维合同

- 普通告警按deployment/alertname/job/instance分组，5秒首次等待、
  10秒更新、4小时重复；恢复也发送。现有业务采样/for时长保持。
- 心跳Q4TNotifyHeartbeat每分钟。接收方必须独立检测3分钟缺失；
  整机宕机/监控链路断开不能靠本机同一通知链路保证报警。
- 接收器以告警fingerprint、startsAt和状态关联，按groupKey更新；
  投递可能重复，2xx表示接收器确认，不能触发模型请求或工具重放。
- Alertmanager通知日志跨同包显式重启复用，活动alert由Prometheus
  再发送；该状态不是可靠任务队列，不能声明崩溃无丢失或exactly-once。
- Q4TNotificationFailures/SendErrors/QueueDropped记录近5分钟失败，
  恢复后历史窗口仍可能触发；AlertmanagerDown与接收端心跳一起定位。
  17条业务告警处置见[运行手册](../../docs/SERVE_OPERATIONS.md)。
- 单组件每代次2×4MiB日志，原始日志不保证脱敏。Prometheus24小时/
  64MB保留不包含WAL/head的全盘硬界；AM通知日志120小时、silence100条，
  同alertname最多32实例、发送队列100。Go内存限制是软界，跨代次磁盘
  留存与整机物理RAM保护仍需另落实。

开发验证HTTP必须遵循项目GPU互斥约定，不能重复执行已有冻结验收
目录来覆盖结果；长稳归用户执行。本批合同见
[短验矩阵](../../docs/SERVE_NOTIFY_GPU_2026-11-04.md)。

只读核验已封存的完整wire/回调与生命周期原件：

```bash
python3 -B tools/serve_notify/report.py verify-archive
```
