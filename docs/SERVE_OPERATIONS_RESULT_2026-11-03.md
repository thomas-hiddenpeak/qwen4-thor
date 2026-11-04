# SRV-12 本地运营能力：限定接受与证据

本地只读诊断、匿名日志投影、诊断包留存工具和 17 条运营告警已限定
接受。[使用与处置手册](SERVE_OPERATIONS.md)覆盖信号口径与固定错误
code；[事前合同](SERVE_OPERATIONS_2026-11-03.md)的预算未扩大。
该交付不表示通知接收器、原始日志轮转、网关鉴权或工业化目标全部完成。

## 身份与变更

实际验收冻结 `52ada55642ea9d5f86ea319f7b9a2e254c8f5dfb`。198 个运行时
源码未改，直接使用已接受的安装程序；没有新编译或默认替换，原零
警告/数值、模板和 64 轮业务证据按身份复用。

| 项目 | 身份 |
|---|---|
| 安装 binary | `84d16983e03c21c62dd249f4c7e9785bb5e62a010e21947778b26b001d84d94d` |
| sidecar | `c520333192c04ddb499dd0188496789ec8ddc20fc5e0f00491ca01f52517aa75` |
| protocol | `aeb66adc439c31b23d943ffe9d9f40253fe2df275f89767b87927ab979bd4bd5` |
| 最终告警文件 | `77a4ddc613b002508be336ab41dfae17c9d9e6421f1638c15ccbfcca0eed637e` |
| 固定 promtool 3.5.0 ARM64 | `d4ff433304144fae3d4490bd381be983e99064c601bc35e8dfe29f52300a9bc4` |
| 同包 Prometheus | `aeebc0ffc6529c87221ab12ff1f9fc276be488869ebc942ce3e4d7ffcfe6a97a` |

Prometheus 官方 tar 与官方 SHA256 清单核对，工具仅保存在本阶段工作
目录。版本固定用于复现，不称为最新生产推荐；没有全局安装或通知。
原 `ed672a65/67897c81` 及完整回退链只读核验，未实际切换默认服务。

## 实际验收

一个 GPU lease 内完成 **3 生成 / 0 负例 / 1 模型启动**，普通 MTP、
strict 工具路径均使用真实模型。硬限仍 4/2/2/1800 秒；业务
133.310 秒、从首个 lease 至 finalize 144.235 秒，无自动重试，
guard `COMPLETED`、child 0、ENV_ABORT 0，不作性能接受声明。

| 检查 | 实测结果 |
|---|---|
| 普通响应 | 精确输出 `710003`；合成私密 ID 实际进入原日志 |
| 项目资料闭环 | 固定 Node24.18.0/openai7.47.0；真实读取 MODEL.md 第1～24行、按 tool_call ID 回填，答案含准确 architectures 与引用 |
| 只读诊断 | before/after/down 分别 HEALTHY、HEALTHY、UNKNOWN，CLI 0/0/3 |
| 身份绑定 | PID2049605、启动 ticks/boot ID、socket inode/namespace、ELF 与两个协议指纹一致 |
| 计数差值 | 同实例 requests=3、success=3、error=0、abort=0；worker started/reaped=2/2 |
| 资源回收 | active/queued/body/strict/preparing/vocab/worker 全归零，slot1/1free；metrics 自身占普通连接1，单列口径 |
| 隐私 | after 投影35事件；原ID、正文哨兵、模型路径及3个取消凭据均未进入三个诊断包 |
| 权限/完整性 | 原包目录0700、文件0600，清单与SHA完整性核对，拒绝覆盖 |
| 健康监控 | 临时 Prometheus 读取19条规则；7次API观测后抓取正常、无 firing |
| 停机监控 | 24次有界API观测，只有 TargetDown firing；首次观测 up=0 至首次 firing 相隔63.038秒，属于采样差而非精确触发延迟 |
| wire | 3份实际响应、9个封套 schema 通过 |
| 清理 | 模型与 Prometheus 均 returncode0、客户端全部退出，GPU释放 |

down 包对五个入口均记录 `connection_refused`，metrics 为 null、计数
差值 unavailable；没有把停机伪装成“全零健康”。工具没有自动取消、
重启、重新生成或重放工具。原始日志仍包含合成 ID，不能把诊断投影
脱敏的结果扩展到原日志或整个验收档案。

10:58:29 CST 复核：owned PID2049605/2050744 均消失、GPU PID空、锁可用，
无恢复标记，MemAvailable116.979GiB、MemFree24.733GiB。它们是清理后
瞬时读数，不是峰值或整机硬内存保证。

## CPU、原失败和实际口径修订

29 项 Python 边界检查与 6 项 driver/身份/留存检查通过。包括坏 JSON/
指标/HTTP、大小/绝对截止、PID变化/归属/协议不符、counter reset、
匿名关联、权限/篡改/符号链接、按年龄/数量/字节保留和仅删除自有夹具。
实际诊断包未运行删除。固定 `promtool` 原组 **25 场景 / 136 断言**
通过，含 for 边界、恢复、禁用、忙/取消、reset、缺失/stale 和实例隔离。

首次预检夹具把 `seq_slots_total` 按后缀误当 counter 清零，未开始测试；
原 traceback 只在工具记录，另保存明确来源的失败 receipt。第二组保留
两项 Python 失败：float 文本替换没有删掉预期样本、Python JSON 深度
行为不能替代明确限额；修正夹具并增加解析前64层/2MiB及非有限小数
拒绝。PromQL 默认向量匹配还造成不同布尔指标相互匹配，已改为显式
按指标名/标签匹配；缺失与 stale/NaN 的夹具语法同时修正。第三组通过。

实际运行后的口径复核又发现：`chat_http.cpp` 把 `/metrics` 计入普通
连接，真实 before/after 都是 business_connections=1。两条 owner
告警原先要求连接数0，会漏报；原 CPU 空闲夹具改0的解释是错误的。
已保留原规则和原验收，改为空闲 active_chats=0 且普通连接≤1。

修订按真实口径复现旧规则3个漏报负控，修复后的 **8 场景 / 97 断言**
通过：worker/body/slot 持有、忙/取消、其他接收连接、两个实际诊断
快照回放。其余15告警和2记录规则字节不变，复用原证明。收尾检查器
误把 `alertname` 子串计入 case 数，原失败保留；仅重新读取已有输出
修正判定，没有重跑 promtool 或模型。

实际临时 Prometheus 加载的是52ada556原冻结规则；最终两条修订的
证明来自原始观测加官方CPU时序，**不称最终文件另起模型实测**。
最终规则 SHA、前后文件及修订源码身份单独封存。

systemd255 对服务 drop-in 的解析通过；宿主两个既有 syslog 弃用提示
保留，不声称整机配置零警告。journald namespace 模板按官方配置核对，
尚未安装，未证明实际日志轮转、磁盘额度或零丢失。

## 证据与剩余范围

[原始证据](evidence/serve-ops-20261103/README.md)逐字节封存135记录为
139文件，共3,661,701字节；6个大工具/TSDB/安装pair只登记本机身份。
[验收摘要](evidence/serve-ops-20261103/acceptance-summary.json)、
[原始业务](evidence/serve-ops-20261103/execution/business/result.json)、
[owner规则修订](evidence/serve-ops-20261103/cpu/owner-correction/result.json)
分开保留，不能用修订覆盖原始运行。
Git blob与原件核对见[收尾清单](evidence/serve-ops-custody-20261103.json)。
139份Git blob、135原件和6个本机产物逐字节/身份核对通过，变更文档
566个链接核对通过。原封存检查器要求其尚未创建的receipt链接先存在，
保留失败并仅延后该固定自引用检查；修订检查器另行封存于
[收尾脚本](evidence/serve-ops-closeout-20261103/audit-custody.py)，未重跑模型。
Git副本不继承私有包权限的保证，原权限由独立记录证明。
此前14个历史失效文档链接仍未修复，不称全库文档检查全通过。

下一优先项 **SRV-12b：观测端点在连接饱和时的可用性**。metrics/config/
capabilities 仍共用普通连接额度，只有健康/就绪/存活与取消使用保留
控制额度；抓取失败也可能只是容量拒绝。需要单独冻结隔离策略和受载
验证，不侵占健康/取消资源，不在本轮改运行时或扩大 GPU 预算。

后续 SRV-13 较长持续运行、退出/重启/发布回退；实际告警通知、原始
日志留存上线、网关/TLS/鉴权/多人配额、通用任务恢复、物理RAM硬保护
及启动边界继续待办。长期工业化目标保持 active。
