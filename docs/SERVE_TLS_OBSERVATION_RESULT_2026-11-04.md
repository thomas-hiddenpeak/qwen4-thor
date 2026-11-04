# SRV-10f：TLS观测与本机通知短验限定接受

2026-11-04。实现冻结`d94cc246`，按[冻结合同](SERVE_TLS_OBSERVATION_2026-11-04.md)
完成开发与本机短验。新host包v2、notify包v3补全TLS依赖；TLS观察器
绑定一个托管v4网关及唯一run，提供只读状态和固定有限指标，显式
notify策略v3接入10条TLS规则。操作见[TLS手册](../tools/serve_tls/README.md)。

## 改动与兼容

- 独立包固定完整源码、解释器和二进制身份，验证包内`python -I -B`
  入口。notify包版本与policy版本分开：新包v3支持原policy v1/v2，
  policy v3才增加TLS目标，并可选host目标。旧包/状态/day保持。
- 观察器只读公开证书/manifest和私有生命周期记录；完整controller、
  run目录和nginx身份固定，替换、缺失、过时、进程或监听不符转
  UNKNOWN。原服务记录不改，不读取私钥、Key或网关配置，不发探测
  请求或服务信号。源停止后仍报告其历史终态。
- 分开证书材料有效期、网关运行状态和观察新鲜度；有效期覆盖全链
  及显式CA。材料有效不代表当前有服务，也不证明远端握手或撤销。
  指标只有受限deployment、generation及有限枚举标签；缺数值不补0。
- 规则覆盖目标缺失/不可达、指标合同、观察过时、源未知、等待/终态、
  临期/无效和未固定信任。新鲜度不合格时不根据旧投影宣告材料有效。
  通知保留generation，避免不同代次混在一起。
- notify早期child已创建而身份/logger尚未建立时，仍回收自有进程。
  短验驱动正常停止失败也先回收辅助HTTP进程，再让后端释放GPU租约。

## CPU与规则证据

57项CPU、4项驱动首轮通过，包含新旧包隔离入口、实际OpenSSL材料、
只读与sticky身份、时钟/失联/终态边界、固定指标和真实自有子进程
异常清理。全部未启动HTTP、模型或GPU。

原规则时序夹具使用promtool不支持的`NaNx80`，解析失败，未执行时序；
同轮规则语法、metrics与生成一致性均通过。保留原日志/源码/结果，
只将该负控改为负时间戳后重跑**29个官方promtool时序场景**，全部
通过。原57项生产依赖身份未变，直接复用，不重复运行。

## 本机真实业务与通知

一次后端模型、一次TLS网关，alpha与beta使用原独立Key。测试证书
由固定显式CA签发，剩余约2天，实际进入7天临期窗口；不是伪造当前
时间。两个客户端通过验证CA/主机名的TLS请求完成同一真实资料查询。

| 客户端 | 输出上限 | 实际输出token | 实际prompt token | 结果 |
|---|---:|---|---|---|
| alpha | 512 | 55 / 58 | 647 / 890 | 2响应、1真实工具，答案及引用正确 |
| beta | 64 | 55 / 58 | 647 / 890 | 2响应、1真实工具，答案及引用正确 |

工具实际读取`docs/MODEL.md`的1至24行，回答包含
`Qwen4ExpForConditionalGeneration`与对应文件行引用。SQLite事件、
4个响应ID及后端完成日志逐一对应，grammar worker启动/回收4/4，
shutdown为0 in-flight。任务未自动重试或重放。

真实Prometheus→Alertmanager→本机TLS接收器得到以下4组确认；原始
webhook body、Bearer校验结果及HTTP204收据均封存，不保存Bearer明文。

| 事件 | 接收器收据序号 | 实际证明 |
|---|---|---|
| CertificateExpiring firing | 1 | 固定公开证书临期被采集并投递 |
| ObserverStale firing | 3 | SIGSTOP自有观察器后，旧静态文本仍保留原时间戳，告警触发 |
| ObserverStale resolved | 4 | SIGCONT后投影更新、告警解除及恢复投递 |
| GenerationTerminal firing | 6 | 网关正常退出后，观察器读到STOPPED并完成终态投递 |

每组保留正确generation，公共标签无路径、PID、证书指纹或凭据。
网关停后原目录全部文件SHA保持。后端一直保留到观察器/exporter、
Prometheus、Alertmanager及本机接收器全部结束，再释放唯一GPU租约。

## 资源与身份

GPU独占**145.757954秒**，136次guard观察无竞争，COMPLETED并释放；
完整阶段**146.956388秒**、25次父控制，均在210/240秒及64次界限内。
采样不能排除短于采样间隔的外部干扰。host记账峰值14.382866GiB，
high/max/OOM/pids事件均0；这不是Thor完整物理RAM计量或硬限额。

11个自有身份退出，scope/全部监听/GPU释放。原锁dev66305/inode3294067
保持且可取得，未删除或替换；容器空，原SOAR恢复unit仍为
masked/inactive/dead/MainPID0。无常驻安装或自动恢复服务。
默认ELF`7619966e`、sidecar、222运行源码、原Key、02k包及三份day
身份保持；day未运行。本批没有修改C++生产程序。

## 可核验材料与使用边界

本机阶段目录`.q4t-work/serve-tls-observation-20261104/`保留私有包和
原始运行记录；`accepted-package.json`标为历史证据、只可显式启用。
测试接收器与2天测试证书仅用于此次短验，不能当作正式LAN配置。

| 项目 | SHA256 |
|---|---|
| plan.json | `3f65f2d20a90c6813a0a30a718d10a772a25313a41efd4695d98dd5fa3df0f2d` |
| 新网关manifest | `06dfb3e6b3542765bbc421c197f161db48123682593e951d7cacd7447b76bf98` |
| TLS观察policy/generation | `46be3d394ed1eef2fcd519ad177aaccd88deb06e855a03f0a8e7132dc6d04728` |
| notify包manifest | `7501de371cc980fdbe1f5ca6f54e43947eeab9250019e50bc0e8f765d067a128` |
| 归档 | `d41d810779569f9e7d9ac4f75887c8890f63cecf0ae6a0122fc94e82933e1c07` |
| 重建结果 | `dbd40f0f6c64cc0470aca15d88c9a23343a580ed05c7727268fad4b2dc59220d` |

[归档](evidence/serve-tls-observation-20261104/records.tar.gz)包含404份
原件，压缩765067字节；[清单](evidence/serve-tls-observation-20261104/manifest.json)
另保留9个本机二进制路径身份。[结果](evidence/serve-tls-observation-20261104/result.json)
离线重建通过，运行与重建源码分别固定，无补充推理。

```bash
python3 -B tools/serve_tls/report.py verify
```

上述入口使用冻结的重建源码与依赖，不启动模型或HTTP。旧TLS/host/
notify包及既有证据保留，回退以它们各自的固定入口运行，不能用新
源码覆盖旧包或将旧包误记为已具备新TLS观测能力。

本次接受限于新包依赖、只读观测及本机通知链路。尚未验证实际LAN
DNS/CA/来源网段、跨主机采集、正式外部接收渠道、签发自动化/撤销、
自然等待证书到期或小时/天级稳定；长稳由用户开展。应用CA/模型协议
迁移、正式容量/备份介质、源盘丢失灾备和完整物理RAM保护仍在队列，
持续工业化目标保持active。
