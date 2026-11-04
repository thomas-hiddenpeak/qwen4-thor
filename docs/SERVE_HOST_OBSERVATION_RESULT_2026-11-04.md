# SRV-12f：host资源观测与告警限定接受

2026-11-04。接受本机只读观测、Prometheus接线与真实TLS工具短验。
生产实现350fa2f6，验收驱动修正至d2c5ea51；业务服务、默认ELF、
协议/模板和02k保护包未变。没有安装常驻服务或部署实际LAN。

## 交付能力

显式绑定v5包和唯一run；首次controller/目录/资源身份锁定。源过期、
失联或身份变化输出UNKNOWN，数值缺失不填零。current为当下内核读数，
final为匹配终态的历史计量，不能把最终清理记录当新GPU探测。
独立loopback nginx输出有界、原子更新的指标；采样暂停时旧时间戳
保留，Prometheus能识别过期。观察器不信号目标服务、不自动重启。

notify显式v2增加host target、10条规则和generation分组；原v1合同
保持。包括源未知、采样过期、退出、OOM/pids拒绝、节流历史和当前
余量。RUNNING与API ready分开；状态合同告警只验证七种state的单选，
不是完整指标schema校验。使用和处置见[手册](../tools/serve_host/README.md)。

## 验证与原失败

- 37有效CPU（17 host、16原通知、4进程边界）、5驱动合同通过。
  22官方promtool时序场景及真实配置/metrics检查通过；后续两组
  定向驱动各3项通过。原5驱动重复冻结不累计成10个独立合同。
- 原nginx语法失败修正私有临时目录；随后空PID断言失败只清理语法
  检查产生的空文件。早期child身份失败暴露清理reader=None错误，
  改为空reader集合；原终态清理未确认记录保留，2项定向复验通过。
- 首次驱动64MiB留存夹具低于128MiB下限，0.133251秒在任何模型/
  监听/GPU启动前拒绝。独立目录改为128MiB/1代次，生产下限不变。
- 第一轮实机1模型/1观察器/1Prometheus、0生成/3控制，因JSON字段
  排序改变low/high行序，驱动误以为投影不一致。原wire名称、标签、
  值和重复计数完全一致；仅修正驱动/报告按完整行集合核对，缺项、
  重复和改值仍拒绝。原总56.587974秒/GPU56.080052秒，guard及同锁
  custody均COMPLETED、已清理；不追认该轮验收通过。
- 修正轮限定GPU150秒/总180秒，完成未执行场景。累计2次模型启动，
  只有修正轮执行1TLS网关、4成功生成/2真实工具。累计GPU185.835368秒、
  实际驱动186.978612秒、33父控制，未扩大210/240秒和64控制总界。

| 真实修正轮 | 结果 |
|---|---|
| 两个独立客户端 | alpha输出预算512、beta64；各2生成/1工具，均COMPLETED/ANSWERED |
| 实际项目查询 | 两次read_project_record读取docs/MODEL.md第1–24行，源码SHA与计划一致 |
| 答案及用量 | 架构名与引用正确；两客户端各55/58输出token、647/890输入token |
| 结构化worker | 4启动/4回收，日志、metrics、SQLite事件与响应ID一致 |
| 实际采集 | RUNNING/current及非零内存进入Prometheus，generation匹配固定policy |
| 暂停与恢复 | 精确SIGSTOP观察器，指标nginx仍服务旧字节，Stale实际firing；SIGCONT后新时间戳及告警解除，整个暂停/恢复观察45.305023秒 |
| 后端退出 | 同一flock副本和独立监控继续覆盖HTTP；真实STOPPED/final、终态告警firing，源目录内容哈希前后一致 |
| host记账 | peak 16,333,574,144字节（15.211826GiB）；high/max/OOM/pids事件均0，不代表物理RAM总量 |
| GPU及阶段时间 | GPU129.755316秒，原guard111观察、custody83观察；总130.257388秒、30父控制 |
| 收尾 | 10个已记录自有身份退出、完整自有进程组清理，1网关代次SEALED；scope/GPU/原锁与端口释放 |

guard的结束记录先于custody退出；不能把第一条lease_released文本
误认为整个阶段锁已释放。实际同一open-file-description仍保持独占，
全部HTTP时间戳位于custody采样区间内。合作锁及轮询不排除采样间隔
内短暂或不可见干扰，不能作为任意外部进程的硬隔离证明。

## 包、证据和回退

| 固定项 | SHA-256 |
|---|---|
| 02k后端v5（复用） | `87980cdf4e990d3f88d7b778a92083a89bd0a983fb180c418e8c6c1c135c35cc` |
| 02k托管TLS网关v3（复用） | `db0aaaa1a2232bc45d671bb7767cec87c95d97e1840be220622dc0e242cc125e` |
| 修正轮observer policy | `2462322d4d64414b5120bbcbcd7cd4a925d792afa54d2332581442c60c07eb90` |
| notify v2包 | `f140ea0d3e0bc7eecaa7538e7e12ee6f022ba71b5262cc37a316a39ab6311608` |
| 修正轮plan | `0ae45fed6825b9e5954a012e9bdde263b3490103d777214b3f9ea7ad033142f8` |
| 622原件归档 | `b14a758ba6858046ccfd1843695c09abd3605d2f1bcb76a1e640aa1c4747ddcf` |

[完整原件](evidence/serve-host-observation-20261104/records.tar.gz)、
[清单](evidence/serve-host-observation-20261104/manifest.json)、
[重建结果](evidence/serve-host-observation-20261104/result.json)。归档1,161,405
字节，7个本机二进制路径身份；经凭据/私钥扫描，归档解包后独立重建
通过，未再次运行模型或HTTP。冻结运行源码与收尾重建工具分开封存。

最初离线报告错误地要求失败版cpu-fix.py包装器等于最终版本；只修正
这条历史版本比较，失败快照自身仍按原SHA验证，最终版本按后续CPU
证据核对。[原错误日志](evidence/serve-host-observation-20261104/archive-original-failure.log)
与[修正说明](evidence/serve-host-observation-20261104/reconstruction-note.json)
保留，没有因此追加模型测试。

本地伴随接受记录位于
`.q4t-work/serve-host-observation-20261104/projection-corrected/accepted-package.json`。
该observer绑定已退出的验收代次；正式新run须新准备观察包，不能复用
旧run路径伪装新服务。notify包仅验证配置与本机Prometheus，未运行
Alertmanager或对真实外部接收方投递；不能作为正式通知渠道配置。

回退只停止自有观察器/监控组件并显式选择原notify v1包；不操作模型
服务或删除原记录。默认pair、原Key、02k包和3份day计划保持，day未跑。
原vLLM恢复服务仍masked/inactive/dead/MainPID=0，容器为空。

## 保留边界

本机短验不等于实际多主机LAN上线、正式接收器确认或长期运行。
实际地址/CIDR/DNS/CA、正式配额/日志访问与备份介质、跨主机独立监控、
完整物理RAM保护及用户小时/天级长验继续待办。没有性能优化接受或
全OpenAI端点/任意模型兼容结论。工业化持续目标保持active。
