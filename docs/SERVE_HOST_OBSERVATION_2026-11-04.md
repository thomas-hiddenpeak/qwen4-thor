# SRV-12f：托管host资源观测与告警

2026-11-04。可靠性/运营推进，不是性能优化。02k保护已在真实内核与
TLS工具业务通过，但Prometheus/通知链路尚不识别该记账域及终止原因。
本批将它接入只读观测与现有通知配置，不能凭本机告警称正式远端部署。

## 冻结范围

- 显式绑定一个v5后端包摘要与独占run路径；允许先启动观察器等待后端。
  首次出现的controller/目录身份锁定，替换、权限/namespace/boot变化、
  过期worker状态及不可读资源均UNKNOWN，不能把历史READY当当前健康。
- 运行时读实际owned cgroup；结束时仅展示匹配的final/terminal证据，
  明确current/final/none来源。缺失数值不填0，不把未启用资源包视为健康。
  控制器消失而终态缺失为UNKNOWN，不自动信号/取消/重试/清理目标服务。
- 固定低基数Prometheus投影与私有status，记录采样时间、观察是否有效、
  生命周期、内核host字节/事件/pids。generation为绑定身份摘要，
  不暴露PID、路径、Key、请求或工具内容。memcg不表示物理RAM总额。
- 单观察器前台运行、独占状态；原子更新有界文件，固定nginx只在
  loopback提供/metrics。源错误仍持续输出UNKNOWN；观察器卡住的旧文件
  由新鲜度告警识别，观察器/采集消失由up/absent识别；不自动安装/重启。
- notify显式v2配置可增加该目标及规则，旧v1包/配置/既有day保持。
  接入真实Prometheus/Alertmanager配置校验；CPU官方时序覆盖正常、
  缺证、过期、终态失败与保护事件，不把接收确认等同人工处理。

## 有界验证

先成组CPU和官方promtool规则/配置检查，保留原失败，只修正受影响项。
核对真实02k的资源/工具证据来源，复用未变后端与内核故障合同。
之后至多1模型/1TLS业务网关、4成功生成/2真实资料工具；一个观察器、
一个独立指标nginx、一个Prometheus实例。最多240秒、GPU210秒，
64父控制调用；单一后端托管guard持锁，不能再套外层竞争guard。

实际核对源运行、采集身份/数值、观察器暂停后的stale/恢复、后端正常
退出后final计量与STOPPED、只读源不被修改、全部自有进程/监听清理。
故障压力复用02k的64MiB/8任务原件与CPU官方规则，不在模型上制造OOM。
不发送真实外部通知、不部署LAN、不执行用户day、不重复未变模型质量。

规范依据：[Prometheus exporter](https://prometheus.io/docs/instrumenting/writing_exporters/)、
[告警实践](https://prometheus.io/docs/practices/alerting/)、
[nginx静态alias](https://nginx.org/en/docs/http/ngx_http_core_module.html#alias)。
工业化目标仍需实际LAN/运营参数、完整物理RAM保护和用户长验等证据。
