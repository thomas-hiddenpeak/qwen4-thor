# SRV-12b 观测连接容量：限定交付

观测入口已从普通业务连接中分离：准确 GET metrics/config/capabilities/
models 共用独立4个名额，健康/取消保留另4个。完整请求头后原子准入，
错误方法、query、前缀不获得观测名额；异常、EOF和排空均归还所有权。
真实业务连接满时可诊断，观测连接满时可健康检查和取消，已经验收并
替换默认程序。[事前合同](SERVE_OBSERVATION_CAPACITY_2026-11-03.md)
的范围和预算未扩张，长期工业化目标仍 active。

## 交付身份与兼容

冻结源码/驱动 `362f42a8c6bca47d853eb47acd9c79f7022885dd`，199个运行时
来源。只改HTTP所有权、观测/配置/能力合同；模型数学、模板、分词、
grammar及MTP策略未改，原数值证据按来源身份复用，不作性能接受声明。

| 项目 | 身份 |
|---|---|
| 默认 binary | `3cd3994d561ba9bcdebcb609204be1056ba16b1fc7bab103447e62bc47ee897a` |
| sidecar | `bb620240dfbbe0437066f6827bf54d00f9e107327f244997fb437fe30727b1b2` |
| protocol | `da1466be364b9d177669a6a6b3209d77ef832bd4c8e059e3d93820bfeb336b05` |
| v4规则 | `849bf0cdeb4de4225c134183a678b8eb7985f50a5fa22ece4dd3c502067e0ff8` |
| 回退 binary | `84d16983e03c21c62dd249f4c7e9785bb5e62a010e21947778b26b001d84d94d` |
| 回退 sidecar | `c520333192c04ddb499dd0188496789ec8ddc20fc5e0f00491ca01f52517aa75` |
| 回退v3规则 | `77a4ddc613b002508be336ab41dfae17c9d9e6421f1638c15ccbfcca0eed637e` |

capabilities observability升级4；config新增分区额度、总处理线程额和
未分类请求头共享边界。私有config v1机器合同和协议指纹随之更新，
固定旧schema的消费者需按实际版本/指纹选择，不能假设所有静态合同
都向后兼容。OpenAI Chat生成输入/输出未扩字段，本轮仍使用已固定
Node24.18.0/openai7.47.0真实工具链。

只读诊断显式识别v3/v4，不把v4缺指标降级成健康。固定投影87→92
series，新增观测owner和4类容量拒绝。v4的metrics自身占观测1、普通0；
两条owner告警以普通0判空闲。旧v3保持普通≤1和原规则逐字节身份；
两套各17告警/2记录，不可混用。参见[运行手册](SERVE_OPERATIONS.md)。

## 实际验收

同一GPU lease先执行原生HTTP23项，再启动一个真实生产候选：full head、
MTP k3、seq1、len8192、prefill2048、inflight8/queued2，仅loopback。
**4生成/136预期负响应/1启动**首轮通过；生成含1主动取消和3成功，
不是4次成功。控制观测预算使用88，含35次Prometheus预留，并非88次
精确实测抓取。硬限6/150/2/256观察/1800秒均未触及，无自动重试。

| 检查 | 结果 |
|---|---|
| 原生HTTP | 23/23，保留原始stdout/stderr；涵盖双向分区、控制满、异常、排空、未分类总额和回收，未加载模型 |
| 业务满 | 活动生成1＋127个合法头/未完body＝128普通连接；额外业务503/capacity_exhausted |
| 受载观测 | 四个GET观测路由200，健康/ready/live200；诊断身份匹配且普通owner128；真实Prometheus读到128且up=1 |
| 观测满 | 四个不同观测路由持有接收连接；额外metrics及诊断三个观测入口503/observation_capacity_exhausted；健康仍200、有效取消202 |
| 回收 | 127个body EOF与4个GET body分别400；半体占用窗口5.757秒，未触发30秒读取超时 |
| 业务恢复 | 普通输出精确710003；真实read_project_record读取MODEL.md L1–L24，再按tool_call ID回填；答案Qwen4ExpForConditionalGeneration且引用正确 |
| 分区计数 | business拒绝1、observation拒绝4、control/unclassified拒绝0；接收capacity5、body_eof127、unexpected_body4，分别对账而不重复相加 |
| 请求/worker | requests4、success3、error0、abort1；worker started/reaped2/2 |
| 结束资源 | active/queued/body/strict/preparing/vocab/worker全部0，slot1/1free；business/control0，metrics自己占observation1 |
| 诊断 | before健康、business-full警告、observation-full未知、after警告；后三者对应故意容量拒绝与读数不可用，不把拒绝delta掩盖成健康或误判恢复失败 |
| 传输证据 | 135份原wire＋2份额外HTTP拒绝body，共388封套离线schema通过；另3个诊断拒绝保存状态/匿名报告/counter，未保留其原响应正文 |
| 安装健康 | 新默认CLI及独立grammar worker握手通过；HTTP复用相同SHA候选，没有为路径变化另起模型 |

业务50.126秒，首个lease至finalize71.203秒；guard COMPLETED、child0、
ENV_ABORT0。1秒进程/GPU轮询不能证明不存在更短暂外部干扰，不把它
当硬件隔离保证。活动生成的真实内容已观察到再取消，没有伪造输出。

临时Prometheus运行5.971秒，测试scrape/evaluation/group interval为
1秒，19条规则解析健康。此处只证明实际指标格式和饱和抓取，不证明
生产15秒规则的实际告警延时；其时序来自下面的官方CPU验证。监控在
填满观测分区之前停止，避免背景抓取引入未预算负例；没有部署通知。

## CPU与保留证据

首组成组检查全部通过：host OFF226/ON235、必需192、后端9、原运营
29＋版本适配6、驱动6；原生配置15项，7个成功形状通过v4投影。构建
零警告，精确路由分类与16线程竞争上限由host检查；库存38字段/10路由、
80来源锚点和3个变异拒绝门保持。官方固定promtool3.5.0的31场景/
169断言通过，含新拒绝类别、缺观测指标、空闲与其他接收连接区分。

[证据目录](evidence/serve-observation-capacity-20261103/README.md)封存
626份原记录为630文件，3,623,356字节；13个ELF/object/bundle/TSDB/
固定依赖和安装pair只保留本机身份。[摘要](evidence/serve-observation-capacity-20261103/acceptance-summary.json)、
[原业务结果](evidence/serve-observation-capacity-20261103/execution/business/result.json)、
[离线封套核对](evidence/serve-observation-capacity-20261103/offline-schema.json)与
[Git字节核对](evidence/serve-observation-capacity-custody-20261103.json)分列。
630份Git blob、626原件和13本机身份核对通过，首轮变更文档562个
链接通过。原始日志/wire含已退出验收实例的合成ID/取消凭据，整个档案不称已脱敏；
Git副本保留字节，不继承私有包权限保证。历史14个失效链接仍另列，
不称全库文档检查全通过。

## 回退与剩余边界

旧pair、构建cache和v3规则在本机
`.q4t-work/serve-observation-capacity-20261103/package/previous/`封存，
更早包保持。回退工具只认已知seal/pair，不要求当前工作源码
与旧阶段相同；已只读核验，未实际切回。若需要回退，使用新鲜lease
目录执行`tools/serve_observation_capacity/stage.py rollback --output ...`，
并为v3服务选择旧规则；本阶段未安装任何常驻规则/通知服务。

收尾只读追溯验证当前及36个历史匹配pair，随后通用检查器因旧sidecar
失配停止；[原拒绝记录](evidence/serve-observation-capacity-closeout-20261103/rollback-reference-audit.json)
保留。核对[10月18日原发布说明](MTP_POSITION_B1A_DEPLOY_2026-10-18.md)
和元数据，发现其明确标记previous_release_was_stale：当时刻意保留
9月sidecar作为历史，专用回退脚本会生成匹配旧A的元数据。旧A 8685d8bf、
cache ca2e60a1、脚本与原发布字节一致，见[边界核对](evidence/serve-observation-capacity-closeout-20261103/rollback-reference-resolution.json)。
因此不能把这份历史sidecar作为第38个匹配pair，也不应把通用遍历的
失败解释为本次破坏历史包。没有执行旧回退、改原文件或补跑模型；
SRV-13通用发布清单须识别该显式历史边界。


11:54:47 CST复核：owned模型PID2094142和Prometheus PID2095290均退出，
GPU PID空、锁可用、无恢复标记，MemAvailable116.346GiB。资源读数为
清理后的瞬时观测，不能当物理峰值/硬限额证明。

未分类请求头仍共享普通+4+4总处理额度，洪泛时连健康/观测也可能被
拒绝；原生测试明确展示边界和恢复，本轮没有入口网络隔离。下一阶段
SRV-13较长持续运行、进程退出/重启和发布回退需要另冻结时间/业务/
故障预算。实际网关/TLS/鉴权/多人配额、告警通知/原始日志轮转、完整
物理RAM/启动保护与通用任务恢复仍待完成。
