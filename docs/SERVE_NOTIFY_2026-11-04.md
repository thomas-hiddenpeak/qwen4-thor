# SRV-12c标准告警通知链路

上一目标回合10b已交付并封存，属于实际进展。本轮补工业化账中的
通知缺口：原Prometheus只有规则与本机查询，没有接收器；开发标准
Prometheus→Alertmanager→HTTPS webhook链路，保持runner/网关及用户
day计划不变。正式接收渠道已异步询问，本轮只向自有本机接收器发送。

## 实现合同

- 固定既有Prometheus/promtool3.5.0与官方Alertmanager/amtool0.34.2
  ARM64二进制，release及sha256sums来源本机保留；复制到私有固定包，
  不全局安装。0.34.2由官方release重定向确认，不宣称以后仍是最新。
- 17条既有业务告警原样复制，新增通知心跳、dispatcher失联、投递失败、
  队列丢弃及发送错误5条规则。实例/部署分组，不发送prompt/工具参数。
- 通知使用HTTPS、CA/域名验证、私有Bearer文件，无URL凭据/query，
  不跟随重定向、不从环境继承代理；默认5秒请求超时、发送恢复通知。
  身份与终态按Alertmanager标准，不把接收端2xx等同人员阅读/处理。
- 心跳每分钟，接收方需在缺失超过3分钟时独立报警；本机宕机或整条
  链路故障不能靠同一链路自报。实际外部接收/缺失检测仍需部署证明。
- 现有告警保持15秒采样/计算及原for；普通分组5秒等待/10秒更新/
  4小时重复，心跳单独每分钟。持久状态与固定包绑定，显式重启复用；
  不承诺exactly-once或通用可靠队列，接收端需按告警身份/状态去重。
  Alertmanager活动alert依赖Prometheus再次发送，通知日志不是任务库。
- 两组件只监听127.0.0.1，关闭Alertmanager默认HA端口，无模型启动/
  停止权限或隐含依赖；固定源码入口、同角色状态flock、parent-death、
  自有进程清理、Restart=no模板。LISTENING仅证明监听身份，健康与
  实际投递另核验；错误与清理分列，失联不伪造终态。
- 单组件每代次2×4MiB日志；Prometheus24小时/64MB TSDB策略不包含
  WAL/head全盘硬上限，Alertmanager120小时notification log保留不是
  无限事故历史。silence100/每条4096字节、同alertname最多32实例、
  队列100、有限抓取/查询资源；Go memory limit为软界，不冒充物理RAM。

[Alertmanager配置](https://prometheus.io/docs/alerting/latest/configuration/)
与[Prometheus配置](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
是标准链路依据；最终以本批固定二进制的配置检查与实际发送为准。

## 冻结验证

先统一CPU输入/配置/包身份/状态互斥/入口/日志与生命周期检查，再做
一组短验：真实TLS接收器先失败再恢复，验证Bearer/CA、重定向禁止、
HTTP失败/成功及恢复payload、心跳/去重和显式重启状态。原始请求与
全部尝试保留，不把受控fixture称为人员实际收到通知。

模型组复用已接受v3包和现有工具闭环：1模型/3生成（普通、真实项目
资料读取、引用回填）；健康时无业务误报，正常停服后原Q4TTargetDown
真实触发并交由接收器确认。后端先停时复制同一GPU租约并独立监控到
全部HTTP进程退出；不另拿锁或中间释放。计划使用冻结规则原for，
不得为了短验修改成瞬时触发。GPU/HTTP阶段整体限360秒、总420秒，
无自动增加模型样本；长测与实际远端接收不在本次自动执行范围。

CPU通过后补充实际驱动的精确进程/HTTP/重试预算并提交冻结，未经冻结
不开始HTTP或模型。阶段失败保留现场，仅重验具体修复影响项。
交付要求证据封存、运行说明、状态与日志、commit/push；不将本项
限定通过作为完整工业化接受，实际LAN/物理RAM硬保护/用户长稳仍待办。
