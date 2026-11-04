# SRV-10f：TLS运行观测、到期告警与独立包依赖

2026-11-04，业务可靠性推进。上一轮SRV-10e已交付5903d0ff，属于
实质进展。继续受控内网/TLS/独立Key范围，实际DNS/CIDR/CA未提供；
本批只做本机短验，不启动day或向外部接收人发送消息。

## 代码发现与冻结范围

1. host观察器和notify的固定源码清单未包含10e新增的tls模块；当前
   工作区导入成功不能证明包内`python -I -B`入口独立可运行。新host
   包v2补齐依赖；新notify包v3固定完整依赖，保留旧策略v1/v2行为，
   显式策略v3启用TLS目标（可同时选择host目标）。旧包及day不改，
   以各自固定入口保留历史能力。
2. 新只读TLS观察器绑定一个托管v4网关包与指定run。核对公开证书/
   manifest、目录和完整controller身份；一旦绑定，不因文件缺失或
   替换改绑。live状态检查owner/子进程/监听，过期或不可验证源记
   UNKNOWN。终态需原owner退出且status/terminal一致；不推导自动
   重启/新代次。只读原服务，不读取API Key、nginx配置或私钥，不
   发送后端/网关信号，不发HTTP/TLS探测请求。
3. 分离“固定包证书材料的有效期”和“当前服务生命周期”。包材料
   可在WAITING/终态时仍被检查；它不证明远端TLS握手、链撤销状态
   或实际LAN可达。新鲜度采用原时间戳及同boot观察时间，缺失不补0。
   固定有限状态/原因/信任配置标签，公开指标不含域名、PID、路径、
   请求内容、Key或证书指纹。复用已验静态loopback exporter生命周期。
4. 新TLS告警覆盖目标缺失/不可达、指标合同/新鲜度、源未知、等待/
   终态、证书临近到期/失效、未固定信任。明确区分采集成功和服务
   健康；新鲜度不足时不据旧状态宣告证书正常。官方promtool验证
   边界、恢复、缺失、错误值和标签隔离。通知保留generation关联。
5. 同组修复notify已确认的早期child身份失败、logger未建立时的
   清理路径，以真实自有子进程验证，不把清理未知记为成功。

## 有界验收与交付

先成组CPU：新旧包独立入口、静态nginx配置、只读和sticky身份、
live/终态/UNKNOWN、时钟与状态边界、有限指标合同、实际自有子进程
清理；不启动监听/HTTP/模型。Promtool使用真实固定二进制运行规则
时序，不把构造源/时钟接缝当作真实证书自然到期。

随后最多1模型、1TLS网关、1TLS观察器及其静态exporter、1Prometheus、
1Alertmanager、1本机HTTPS夹具接收器；同一后端生产guard持有完整
阶段（包括网关停后观测），最多240秒/GPU210秒/64父控制。
alpha512/beta64各2成功生成1真实资料工具，检查正确答案/引用/独立
预算和资源释放。实际签发处于7天预警内且启动大于1小时的证书，
核对真实预警采集/本机投递、观察器暂停后的Stale及恢复、网关正常
退场的终态告警；后端保持到所有HTTP结束后再停，不额外持第二把锁。

保留失败原件；仅具体失败后定向修正。归档原日志/任务/观测/规则与
包身份，离线重建；报告源码与运行源码分别固定，终态已清空字段
须以原始启动身份记录对照。可验收后沿用授权commit/push。

本批不是性能优化，不推导五档非劣或小时/天级稳定；不验证证书
自动签发/OCSP/CRL、真实LAN、跨主机监控或正式外部通知。
依据：[Prometheus指标建议](https://prometheus.io/docs/practices/instrumentation/)、
[告警规则](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)、
[官方规则时序测试](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)。
