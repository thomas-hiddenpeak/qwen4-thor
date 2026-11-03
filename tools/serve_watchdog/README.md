# 独立接收端心跳检测

这是接收器的持久化适配器与独立观察器。生产入口不监听HTTP、不向
任何地址发送通知，也没有模型/GPU/服务启动权限。部署时应放在被监控
Thor之外的接收端；只有实际外部采集和通知渠道接通后，才能宣称整机
失联可通知人员。本机短验的接受范围见STATUS。

固定接收Alertmanager v4、receiver=`q4t-webhook`、唯一告警
`Q4TNotifyHeartbeat`，deployment必须匹配；普通告警必须交给原告警
接收器，不能以本工具的返回值替代它们的持久化接收。

## 准备与运行

接收端以0600文件提供随机Bearer凭据，和发送端使用同一凭据。父目录
必须已存在；在本仓库开发时，所有实例放在`.q4t-work/`或`build/`。

```bash
python3 -B tools/serve_watchdog/watchdog.py init \
  --state .q4t-work/watchdog/state-001 --deployment thor-internal \
  --token-file .q4t-work/watchdog/token-private
```

init保存固定源码、Python身份、私有凭据和policy，返回完整
`policy_sha256`。将下例DIGEST换成该值，使用输出目录中的固定入口。

```bash
/usr/bin/python3 -I -B \
  .q4t-work/watchdog/state-001/source/tools/serve_watchdog/watchdog.py watch \
  --state .q4t-work/watchdog/state-001 --policy-sha256 DIGEST
```

watch仅前台运行，Ctrl-C/SIGTERM停止；同一状态同时只允许一个watch。
`unit`输出Restart=no的systemd模板，不安装、不enable。显式重启使用
同一state与policy，不重新init；已有state永不自动覆盖或恢复。

## 接收器适配

可信HTTPS接收器在完成传输边界检查后调用包内
`watchdog.ingest(state, digest, raw_body_bytes, authorization_header)`。
它再次验证Bearer与完整心跳合同，在文件及目录fsync后才返回
`durable=true`；异常必须返回非2xx。不要记录Authorization或原body。
`ignored_stale_incident`表示收到更早的startsAt，不刷新活跃时间。
其他告警由原接收器负责。接收器必须有自己的连接、body、超时与访问
限制；本工具不是通用HTTP服务器。验收用HTTPS夹具不作为生产入口。

需要进程隔离时，`ingest`子命令从stdin读取最多64KiB原body，
`--authorization-fd FD`从继承的FD（≥3）读取header；不要把凭据放在
argv/环境变量中。父接收器须限制子进程数和执行时间，并检查退出码。

## 判定与故障边界

- 首次init即开始180秒等待窗；从未收到则MISSING。每个已认证且合同
  正确的firing到达后180秒无新到达即MISSING。resolved立即不健康。
  恢复后的有效firing重新PRESENT；普通告警/错误身份不刷新时间。
- AM重复心跳可能字节完全相同，startsAt不随心跳更新、firing的endsAt
  为零；不能按payload/fingerprint去重。它证明投递到达，不证明此刻
  Prometheus仍在采样。缓存、重试、已认证重放或同incident乱序不能
  被标准payload区分；源故障到最后投递的延迟另算，不能宣称3分钟内
  必然发现一切源故障。更早incident忽略依赖发送端startsAt顺序。
- 使用Linux CLOCK_BOOTTIME（包括休眠），接收器进程重启不重置时间；
  换boot或时钟回退记UNKNOWN，新boot的新有效心跳才能重新证明活跃。
  墙钟仅供展示和外部采集新鲜度；接收端墙钟校时不延长存活窗口。
- 固定state文件、64条状态转换环及累计计数，旧转换被挤出会计数，
  不宣称无限事故历史。写入失败不确认，残留`.new`保留并拒绝覆盖；
  损坏/权限/链接/身份异常不自动修复。不要删除锁文件规避互斥。

`status`只读实时判定，`metrics`输出实时Prometheus文本；命令成功
表示观察成功，是否健康须读`healthy`。watch每秒原子更新
`status.json`和`metrics.prom`，文件都是私有的。可在接收端用受控
collector读取，或明确配置node-exporter textfile目录及只读权限。
不要直接把状态目录开放给网络客户端。

外部独立Prometheus加载
[`q4t-watchdog.rules.yml`](../../deploy/serve-ops/q4t-watchdog.rules.yml)：
检测MISSING/RESOLVED/UNKNOWN、观察时间戳超过15秒未更新或超前30秒、
以及整个指标缺失。采集器停止后旧文件仍存在，必须启用新鲜度规则。
多deployment还需按预期清单分别生成absent规则；删除目标配置不能
被这份通用规则自动发现。独立采集/告警器自身失联也要有另一故障域
覆盖，不能递归声称本机观察器可保证自己整机故障时发出通知。

默认runner、网关、既有通知包及用户day计划不由本工具修改。
