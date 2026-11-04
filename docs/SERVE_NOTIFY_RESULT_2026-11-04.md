# SRV-12c本机通知与真实服务恢复短验

Prometheus→Alertmanager→HTTPS webhook链路已完成开发及本机限定验收。
真实模型停服触发Q4TTargetDown，服务显式恢复后收到同一告警的resolved；
接收失败重试、重定向拒绝、通知状态复用及真实资料工具闭环通过。
正式外部通知接收方、独立心跳缺失报警和常驻安装尚未部署，不能把
本机204确认当作人员已收到或处理。工业化目标继续active。

[开发合同](SERVE_NOTIFY_2026-11-04.md)、
[HTTP前冻结的实际矩阵](SERVE_NOTIFY_GPU_2026-11-04.md)与
[接入说明](../tools/serve_notify/README.md)分别保留。首版开发计划1启动/
3生成在任何HTTP前细化为2启动/4生成，用于证明真实恢复，未在运行中
扩预算。实现与驱动冻结`b166cee7`，实际一次完成，无修复重跑。

## 交付内容与身份

- 私有固定包、HTTPS/CA/域名验证、Bearer文件、不跟随重定向/环境
  代理、恢复通知、每分钟心跳；原17业务规则保持，增加5通知规则。
- 显式prepare/check/init-state/run/unit/status/stop，包内固定源码，
  状态与包绑定、每角色flock继承、parent-death、owned进程清理、
  2×4MiB组件日志；Restart=no模板，无系统安装或自动启用。
- 两组件均只监听loopback，关闭AM默认HA端口，不自动启动/停止模型。
  LISTENING只说明监听身份，实际ready和投递已在本批单独验证。
- 固定Prometheus/promtool3.5.0沿用既有官方产物，Alertmanager/amtool
  0.34.2从[官方发布](https://github.com/prometheus/alertmanager/releases/tag/v0.34.2)
  下载并校验同源SHA256；压缩包`5aaad82e...fdd0b0`，AM二进制
  `62198848...6c1c7`。这里只确认本批来源/完整性，不声称已另验签名。
  API发现遇403后使用官方release重定向，记录保留。
- 通知包manifest
  `67b4a7e629a55e112205c5ed154a2b10a5e200eef1c53c29507f1efc8f15700a`。
  模型使用已接受v3包`160796c3`的原ELF与固定参数，经外层同一guard
  直接启动两代；本项不改变生产controller或给它增加恢复权限。

默认pair仍为`d66f2bb9`/`42d7672a`，protocol`55a3e67b`及211运行时
来源保持，既有10a/10b源码不改。无新模型数值算法、编译或默认替换。

## 验证结果

CPU16项、驱动4项及2个隔离入口首轮通过。涵盖严格配置/私有凭据/
包身份/状态互斥、实际promtool/amtool语法与systemd模板、正常退出/
日志失败/意外退出的真实子进程回收，以及通知身份、截断与租约门禁。
未启动HTTP或GPU的CPU记录与原件全部保留。

| 实际场景 | 结果与证据边界 |
|---|---|
| 首个心跳接收失败 | 首次503，随后同fingerprint重试204；正常心跳再次送达 |
| 合成probe重定向 | 307未跟随，/redirected访问0次；接收器恢复后原地址204 |
| AM显式正常重启 | 停止后nflog落盘，同包/状态重启，再提交相同probe；20.007秒内无重复确认，随后resolved送达 |
| 通知失败自身告警 | Q4TNotificationFailures实际触发并收到204；窗口表示过去5分钟发生失败，不等同当前仍不可用 |
| 真实模型业务 | 第一代普通+真实项目工具/引用3成功，监控未报告业务故障；第二代恢复普通1成功 |
| 真实模型停止/恢复 | 保持原1分钟for，停服后TargetDown先503后重试204；显式启动第二代后，同fingerprint和startsAt的resolved收到204 |
| 生命周期与资源 | 1 Prometheus/2 AM代次正常停止；状态锁实际继承到Go进程、无自动重启，后端/监控/接收器进程与监听全部退出 |

回调共10次：**7次204确认、2次503、1次307**，全部Bearer正确、payload
完整、truncatedAlerts=0；合成probe与真实TargetDown分开取证。通知
投递重试没有触发模型/工具重放。第一代nflog仅保留当时hash/大小记录，
之后正常复用并更新；没有第一代完整nflog副本，不能拿最终文件冒充
初次状态。最终状态仅在本机按身份保留，完整回调可独立复核去重窗口。

模型2启动/4生成全部成功，1次真实`read_project_record`读取
docs/MODEL.md第1..24行，SHA/文本与真实文件一致；回填回答
Qwen4ExpForConditionalGeneration并附`[docs/MODEL.md:L1-L24]`。
第一代3总/3成功、grammar2/2回收，第二代1总/1成功，owner归零；
4个请求各一条completed/released=1 trace。普通0.429秒、工具3.745秒、
回填4.024秒、恢复0.432秒，只作业务观测，不作性能非劣声明。

驱动275.635秒，完整GPU/HTTP租约276.843秒；13显式控制、guard261
记录/257观察、COMPLETED/exit0，均在360秒冻结限额内。两个模型代次
与中间停服窗口同锁；采样未观察到竞争，不排除短于采样间隔的干扰。
Prometheus抓取保守上界78另列，内部发送以实际metrics和接收回调为
证，不把保守上界或7次确认当成全部HTTP尝试次数。

## 证据与待办

[266份原件目录](evidence/serve-notify-20261104/manifest.json)封存7份
Git文件508336字节，37份本机私有材料/二进制/持久状态只记身份。
已逐项扫描已知凭据；Bearer/私钥/URL文件/私有policy与验证日志不
入Git。两代完整模型wire、全部10回调、CPU源码、告警/进程/guard
记录保留。只读复核入口，不新增模型或HTTP：

```bash
python3 -B tools/serve_notify/report.py verify-archive
```

终检GPU空、原锁inode3294067可取得，容器空、SOAR仍masked/inactive；
没有本阶段常驻unit。默认pair及用户day身份检查通过，day未执行。

本次实际只向本机自签TLS接收器发送；外部CA/通知渠道、接收方3分钟
心跳缺失检测、人员处理流程与实际网络故障尚需部署证明。新增5条
规则均过真实语法/指标名检查；dispatcher长时间失联、发送队列满和
全部发送错误时序未逐一实测，不把这次接收端故障替代全部监控故障。
重启去重仅证明20秒正常退出窗口，未证明崩溃零丢失、4小时去重或
exactly-once。CPU日志故障/轮转基础能力与短验分列，不外推全盘留存。
实际LAN接入、跨代次日志/磁盘策略、整机物理RAM保护、用户长稳仍在
工业化验收账中待办。
