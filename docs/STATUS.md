# 当前状态（2026-11-23）

Serve总目标仍为 **active**。按[完整计划](SERVE_MASTER_PLAN_2026-11-08.md)
和[逐项任务表](serve-master-goals-v2.json)完成全部33项任务；阶段验证、
发行和PR合入都不是总任务终点。非性能改动只做受影响行为的针对性
验证与必要业务短验，长稳实跑与已延期听感由用户负责。

## 正在推进

同身份的官方 SDK 已能旁路接入进行中的 Live WebRTC 通话，断开期间主端可继续控制，重连按原 ID 回放最近三秒的事件。修复了旁路关闭时主端可能丢失终态的问题。修复后的短验为 38.89 秒，主端收到终态，旁路正常关闭，随后 Chat 返回 42，资源已回收。下一项是已存储会话的 fork，其后继续 SIP 和可回退发行。[两轮实测](evidence/serve-live-sideband-20261123/README.md)。

Live WebRTC HTTP 创建已通过真实 SDK、受限前端与项目资料语音闭环。一次 GPU 启动 66.40 秒，原生 TLS 接收 SDP，通话中同客户端再次创建收到 429，模型转录正确、回填后播报 rev426，关闭后普通 Chat=42。已释放客户端名额、主机预算和 GPU 租约。继续同身份旁路、fork 和 SIP 控制。[验收与边界](evidence/serve-live-media-create-20261123/README.md)。

Live 增加 WebRTC 前端事件权限校验及原生 PCM 规划。验证了空名单、精确嵌套 Responses 选择、256 项边界，以及协商格式和公开会话字段的区别。13 项针对性 CPU 检查通过；服务端 HTTP 创建、实际通话的权限执行和旁路仍在接入。[权限与格式验收](evidence/serve-live-media-policy-20261123/README.md)。

修复原生网关与模型对 Live 显式 WebSocket 子协议选择不一致的问题。两个进程使用共同函数选择 `live` 或 `realtime`。8 项 HTTP 检查、9 项实际入口进程检查和 1 项打包来源检查通过。真实模型和原生 TLS 验证了三种 101 握手，Live 正常启动和关闭，Chat=42。单次启动 36.60 秒，已释放 GPU。[握手与验收](evidence/serve-live-subprotocol-20261123/README.md)。

WebRTC 独立媒体进程已在 Thor 上完成回环 ICE、DTLS 证书绑定、SRTP 双向音频和 SCTP 命令验证。Opus、PCMU、PCMA 均在关闭数据通道后继续传音；无插件、错误证书和启动超时均已验证。此阶段还未接入 Live HTTP 创建及模型。[传输实测](evidence/serve-live-webrtc-media-20261123/README.md)。

已补齐语音、追踪、Decisions 和录音的公开协议。90 个 OpenAPI 操作覆盖运行时 91 项路由，转录由同一路径的查询参数选择。原生及可选 nginx 入口均已核对 73 个 HTTP 模板，三种 WebSocket 握手通过可选前置。修复了已存储 Chat 音频与缓存标记的悬空引用。[协议与入口验证](evidence/serve-voice-public-contract-20261123/README.md)。

Live 录制已通过真实 SDK 和项目资料语音闭环。下载 655,107 帧 WAV，双声道逐样本与原始输入和线上输出吻合。发布前及跨客户端均为 404，正常关闭后 Chat=42，GPU、端口和锁已释放。接下来是媒体会话旁路、fork 和原生传输。[真实录音验收](evidence/serve-live-recording-http-20261123/README.md)。

Live 会话的显式录制、隔离存储和同身份 WAV 下载已接到原生入口。16 项原生 CPU 检查、4 项部署配置检查通过，存储写入失败后已有会话继续完成语音。[存储与故障边界](evidence/serve-live-recording-store-20261123/README.md)。

录制链路的双声道 WAV 编排已通过 6 项 CPU 检查，支持 PCM 与 G.711。正在接入持久化和下载。[波形验证](evidence/serve-live-recording-wave-20261123/README.md)。

Live 显式与语音委派、双向转录触发已通过真实 SDK 短验，四次分类和两次语音模型执行共用同一客户端的单个数据名额。项目查询、语音 rev426、正常关闭及后续 Chat=42 均通过。继续可选录制、内容下载、旁路与 fork。[主连接验收](evidence/serve-live-decisions-20261123/README.md)。

原生 `POST /v1/decisions` 已通过真实 OpenAI SDK 多题、正反判断、缓存与计量短验。第二次请求命中 1,454 个共享 tokens，三个请求结算 3,660 tokens，后续 Chat 回答 42。27 项 CPU 检查通过，默认仍为 rev426。[业务证据与原失败](evidence/serve-decisions-http-20261123/README.md)。

Decisions 标准请求与答案合同通过 14 项 CPU 检查，包含工具输入归属、强类型选择、有序评分、单题拒答和真实用量字段。模型取分、HTTP 执行及 Live 自动触发继续。[合同与验证](evidence/serve-decision-plan-cpu-20261123/README.md)。

Live Decisions 已接入会话条目的追加、删除和快照。7 项针对性 CPU 检查通过，实际引擎零警告构建，新增 GPU 启动 0。模型评估和转录触发器待接通。[条目合同与证据](evidence/serve-live-decision-items-cpu-20261123/README.md)。

Live 服务端 Responses 已通过真实原生 TLS/WSS 工具闭环。客户端实际读取项目资料并回填，后台文本和前台语音转录均为 rev426，收到 107,520 字节 PCM，正常关闭后普通 Chat=42。三次查询客户端准入，结算 1,279 tokens。一次独占短验 61.930 秒，63 次 NVML 正常并释放资源。前一轮 NVML 中止的原始记录保留。此前 70 项 CPU 验证仍有效；听感待验，默认发行保持 rev426。继续 Decisions 和其他计划项。
[真实业务验收](evidence/serve-live-responses-http-20261123/README.md) · [CPU 检查](evidence/serve-live-responses-execution-cpu-20261123/README.md)。

Live 委派的事件包装与工具回填通过 13 项 CPU 检查。真实 Responses 输出器的逐项
完成事件决定可回填的调用；程序父调用、MCP 审批 ID 和客户端工具结果分别核对。
提前续接须等工作线程退出，后来出现新调用则需要补结果并再次续接。
正在绑定原生请求执行入口。[续接记录](evidence/serve-live-responses-continuation-cpu-20261123/README.md)。

Live 服务端 Responses 委派配置通过 27 项针对性 CPU 用例。客户端收到的会话
快照移除 MCP 凭据；无效配置在确认后报告并关联原命令，修正后同会话可继续。
旧校验无法放行新设置。实际模型委派、工具回填与语音回传仍待接入和短验。
[配置与验证记录](evidence/serve-live-responses-settings-cpu-20261123/README.md)。

原生 Responses 执行器事件通道通过14项针对性CPU检查，覆盖文本、工具、
用量、背压与错误终态。服务端委派继续接入配置和工具结果续接。
[原始记录](evidence/serve-live-responses-stream-cpu-20261123/README.md)。

[Live音频播放与业务恢复短验通过](evidence/serve-live-fixture-http-20261121/README.md)：
1启动/1片段/4工作/68.022秒，65次NVML正常且资源释放。实际播放逐字节
一致，播放中/显式静音输入未引发额外工作；解除静音后完成正确ASR、
资料读取/回填、语音rev426、正常关闭和Chat=42。原RPM3保持，1717 tokens
结算且预留归零。已有21项CPU与5项驱动反例。累计4启动/14工作/323.305秒，
旧失败保留；听感未验。默认未改，继续Responses/Decisions及全部master。

[Live主WSS客户端真实闭环已验](evidence/serve-live-client-http-20261121/README.md)：
正确ASR、真实资料委派/回填、语音rev426、正常closed/1000及后续Chat=42。
1启动/4工作/64.040秒，原RPM3下无额外ASR，1730 tokens结算/预留归零，
62次NVML正常且清理完成。最后的旧健康断言错误要求ASR同时常驻；
原FAIL保持，5项反例与独立原件复核通过，复核新增GPU0。累计三轮
3启动/10实际工作/255.282秒。继续fixture、服务端委派及其余Live/master
任务；默认rev426保持STOPPED，尚未发行本候选。

主连接及关闭修复已有118项不同原生CPU场景；[主连接绑定](evidence/serve-live-binding-cpu-20261121/README.md)
与[关闭尾部18项针对性验证](evidence/serve-live-close-tail-cpu-20261121/README.md)
通过，实际引擎68bcc25e构建零警告。SDK合同、生命周期、连续输入、
输出/工作线程、调度及客户端上下文的逐组原件见
[Live阶段计划](SERVE_LIVE_2026-11-21.md)。早期夹具/业务失败均保留；
本次只接受主连接客户端资料闭环，未验其他委派、录制/媒体或完整兼容。

[原生Realtime追踪](SERVE_REALTIME_TRACING_2026-11-20.md)已通过
[主轻实际TLS/SDK业务](evidence/serve-realtime-tracing-http-20261121/README.md)：
2模型启动、6实际生成、159.604秒，149次NVML正常，租约与资源清理完成。
两次真实资料读取、引用回答、持久化、独立scope和删除不复活通过；
写入目录失效与追踪子进程退出均有界恢复，健康核心PID不变。
含前两次原始失败累计4启动/9实际生成/260.147秒，原失败保持。
[资格与封包15项CPU闭合](evidence/serve-realtime-tracing-qualification-cpu-20261121/README.md)，
两档完整历史记录可逐字重构，新增追踪见证仍满足8 MiB上限。
原会话身份、空闲重试修复及原夹具失败均保留；模型质量旧FAIL不改写。
主轻资格已签发；六个资格/封包来源经语义复核，22项清单反例通过。
[rev426已可回退交付](evidence/serve-realtime-tracing-delivery-20261121/README.md)：
19条原生命令通过/422.580秒，实际整根回退和再前进、客户状态及预算保持。
当前STOPPED；[PR #57已合入main](evidence/serve-pr57-merged-20261121/README.md)，
合并提交`22669a162`，最终两条检查通过。继续Live及全部任务，
未扩展性能/全模型质量矩阵。

PR #56的CI容量回归及旧版封包清单已修复，
[24项针对性CPU](evidence/serve-realtime-mcp-ci-capacity-20261120/README.md)、
[主轻真实MCP短验](evidence/serve-realtime-mcp-binding-http-20261120/README.md)及
[11项资格边界CPU](evidence/serve-realtime-mcp-binding-gate-20261120/README.md)通过。
含首次驱动漏参失败共3启动/5实际生成/159.322秒，150次NVML正常；
修复后两条资料回答通过，原质量FAIL保持。
[rev416已包含修复并可回退交付](evidence/serve-realtime-mcp-binding-delivery-20261120/README.md)，
19条管理命令通过，350.683秒，客户状态及恢复预算保持、服务STOPPED。
[PR #56已合入main](evidence/serve-pr56-merged-20261120/README.md)，
合并提交`60f5ae908`；两条最终检查通过。未变恢复行为复用原身份证据，
没有重跑。继续原生追踪、Live与全部剩余任务。
[审计清单遗漏已修复](evidence/serve-realtime-mcp-inventory-review-20261120/README.md)：
39个来源及3条Realtime描述经代码/证据复核，20项反例检查通过；0GPU。

[G00范围决定已落实](evidence/serve-scope-decision-20261120/README.md)：
完整推理与应用服务保留，平台管理API按用户决定排除；157条冻结
路径归入实现任务、88条排除，分类不代表兼容通过。总目标继续active。

Realtime MCP的定义、目录、参数流、审批/调用、历史隔离、公开会话
及主轻资格已完成；逐组原始CPU/GPU记录见[阶段计划](SERVE_REALTIME_MCP_2026-11-20.md)。
[实际恢复五组合通过](evidence/serve-realtime-mcp-managed-recovery-20261120/README.md)：
8启动/22工作（含8自检）/9连接/2探测，含原零模型失败共617.179秒，
580次NVML正常且清理完成。实际读取项目资料5次、完成历史认证2次；
TLS恢复保留健康核心，控制器恢复等满配额窗口，整包回退保留能力。
确切默认策略被拒绝的MCP访问不影响普通问答；没有自动重放。

协议/恢复通过，**生成质量仍为FAIL**：配置回退后的一个标题漏掉
“架构笔记”四字，原答案保留，主轻原错答/拒答也保持失败记录。
[Guardian准备子树修复](evidence/serve-mcp-guardian-subtree-cpu-20261120/README.md)
已有7项原生CPU及实际封包准备、命名空间检查；死亡/停止仍完整清理。
新默认候选`17fc1464`及保留包`25ebd972`均实测，资格registry为
`5a44b4a3df68c45cb34f0b0758b51aeb4acd556a98d549db3992a106cb159fa3`。
原TLS证据保留原身份，仅复用未变行为。
[rev406已可回退交付](evidence/serve-realtime-mcp-delivery-20261120/README.md)：
19条管理命令通过，实际整根回退/再前进、客户状态及预算保持，服务
STOPPED。继续main收尾，然后tracing、Live和其他全部任务。

PR #55合入前发现5项旧音频断言与来源标记表示不一致，已修复并完成
[10项针对性CPU验证](evidence/serve-realtime-duplex-ci-boundaries-20261120/README.md)，
运行时未改。代理误将内部网关初始凭据纳入公共恢复证据，当前公共
副本已清理；历史值按已披露处理。[rev396重新签发并可回退交付](evidence/serve-realtime-duplex-credential-reissue-20261120/README.md)
完成，5项认证CPU与19条管理命令通过。当前和自动/手动回退包均已
更换初始值；客户Key、配额、用量与引擎保持，服务STOPPED。原验证
和错误回答均保留，Git历史中的旧值不再作为当前包的初始化凭据。

[rev386双向音频已可回退交付](evidence/serve-realtime-duplex-delivery-20261120/README.md)，
服务保持STOPPED。19条原生CLI完成升级、登记、整根回退和再前进，
新增GPU启动0，客户配额、用量、profile历史与恢复预算保持。主线
已包含[PR #55的完整双向音频与凭据更正](evidence/serve-pr55-merged-20261120/README.md)，
合并提交`abf4dab67`；两条最终检查通过。MCP后续交付见上文；继续
追踪、Live及全部master任务。

同一conversation已接通实际音频输入/ASR、手动commit、VAD自动回答
和打断、空闲、混合及临时音频，组合语音输出、函数工具与持久Prompt。
输入历史、迟到结果、取消用量与清理均绑定实际所有者；不引入额外
外部语音服务。开发细节与原始失败保留在[阶段计划](SERVE_REALTIME_DUPLEX_2026-11-20.md)、
[日志](log/2026-11-20.md)和[逐项任务表](serve-master-goals-v2.json)。

- [取消分类修复](evidence/serve-realtime-duplex-cancel-usage-http-20261120/README.md)：
  实际VAD取消保留文字100/音频336，连同输入与ASR共737 tokens准确结算。
- [主轻资格](evidence/serve-realtime-duplex-composed-profiles-20261120/README.md)：
  确切原业务加当前修复证据组合；主档旧身份来源复用明确记录，未重跑全矩阵。
- [真实恢复](evidence/serve-realtime-duplex-managed-recovery-20261120/README.md)：
  含原失败共6模型/21工作/7连接/1探测、353.339秒，332NVML正常并清理。
  TLS恢复保留健康核心，控制器恢复等满配额窗口，整包回退保留双向能力。
- 原质量FAIL、字段检查失败、最终metrics缺失及命令格式差异均保留，
  不追认原失败；用户允许模型质量与协议/恢复分别记录。未改默认采样，
  未追加性能、完整模型质量或长稳矩阵，听感仍待用户安排。

## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev426；服务包`41760222c76714a23e5753e53ef9e86b3b91e9c0a4f42373f04bf845d3b34afe` |
| 引擎 | `94df51e5baa135d2fe61009d31212191269d9549068de1b78b19590c4e0a717c` |
| 协议 | `03b227b161b55f1ea4e310745226f47aaaca41853b3bb222e95a7b0718cd37ae` |
| 引擎发布记录 | `68d5005b3f1054f6ae785340de90aee86e6505e7722d28734a4f21ff13b9624e` |
| 原生入口 | `8bd79b7a2c998138934541e6e2813d1d778d908cc84c86abec65ba843f954935` |
| 状态 | STOPPED；本阶段没有启动生产服务 |
| 客户限制 | alpha RPM30/TPM6000/输出3457；beta RPM1/TPM6000/输出256；保留动态值与已消费预算 |
| 主轻资格 | `2603a172a402f5a7eb8e2db5d7da897fa84ae3990a26a3afc502ed946951fb71` |
| 保留控制包 | `20871e8a81691446dda42b49429fffff44b12322088f290c5f92333f27870015`；同一追踪引擎/入口和Guardian，只切换已验旧控制器 |

发行完成19条原生管理命令，包括实际升级、STOPPED整根回退和再前进。
追踪采集/持久化/删除及独立故障恢复补了确切新引擎和入口的主轻业务；
未变监督器/TLS实现/后端恢复按原身份证据复用。没有另跑完整运行
故障矩阵，原质量FAIL保留。主轻记录逐字重构完整旧资格，8 MiB上限保持。
旧rev386/rev376和历史发布原件保留；旧披露初始值不得手动复用。

当前Realtime已交付独立转写会话、语义VAD、文字/函数工具对话、
持久Prompt、原生语音输出、同会话双向输入、MCP及原生追踪。Live及其余
矩阵仍是实现任务，不能宣称完整Realtime或完整OpenAI兼容完成。
原答案/模型质量、数值边界、声纹与驱动失败保持各自记录。

## 全部任务进展与剩余缺口

详细内容与原始证据以[任务表](serve-master-goals-v2.json)为准。

| 范围 | 当前状态 |
|---|---|
| G00 | 用户范围决定已落实；完整推理/应用服务必做，平台管理API不纳入 |
| A03—A06/A08 | 采样、工具控制、结构化输出、Completions/Models与持久会话已交付 |
| A01/A02/A07/A09—A12 | 已有大量实际业务交付；剩余字段/媒体/内建后端、Live及完整客户端组合继续 |
| N01—N07 | 原生接入、TLS/CORS/Key/配额、HTTP、运维和发行已交付；nginx/systemd为可选集成 |
| R01—R06/R10 | 辅助故障隔离、自监护和已验恢复已交付；新增能力的恢复组合继续 |
| R07—R09 | 冷准入、物理DRAM边界及压力/宿主组合仍需完成；未获维护交接不干预其他服务 |
| Q01 | 项目资料查询真实闭环已交付，新增后端/客户端/故障组合继续 |
| Q02/Q03 | rev426已可回退交付，追踪PR #57已合main；最终矩阵和长稳交接继续 |

辅助依赖失能不能连带停止健康核心业务；监控恢复不自动重启模型，
真正设备故障仍保留保护。真实云账户验证依赖部署方配置，不能把
本地合同测试写成真实账户验收。长稳工具/交接仍属本任务，实际长跑
由用户开展。

## 工作区、证据与保留候选

有效工作分支仍为`codex/serve-resilience-cd`，已快进对齐PR #57合并
提交；Live合同/配置、生命周期/传输及连续输入已分组提交推送，
输出/工作线程继续形成独立提交，工作分支仍承载Live实现。仅在独有提交、未提交内容、活动PR和证据
路径均核对后整理重复分支，不删除其他任务内容或已发表回退包。

为当前构建恢复了591个host对象，未覆盖后续更新的runtime库；旧
CPU夹具和退役构建库的可逆归档、SHA和恢复脚本保留在
`.q4t-work/serve-realtime-duplex-20261120/`。当前模型与reference目录
保持只读，默认包已在上述验收后显式发布，动态客户状态与预算保持。
磁盘余量不足时另将67个退役CPU测试程序精确压缩归档，释放
188854224字节；当前host测试、runtime和发行包保持，原路径及
逐字节恢复方法见该目录的`retired-host-tests-03/plan.json`与`restore.py`。
本MCP阶段另对14个本阶段退役CPU程序可逆压缩，释放335235523字节；
当前host/runtime/default未改。原路径、SHA和实际恢复核对记录见
`.q4t-work/serve-realtime-mcp-20261120/retired-host-tests-01/`与`-02/`。
其后`-03/`和`-04/`另可逆归档22个退役CPU程序，释放523849368字节，
原SHA、恢复脚本及实际恢复核对保留；当前host/runtime和发行包未改。

另将14个2026-10-31及以前历史包的同字节head副本转为只读共享inode，
保留全部原路径和SHA，释放4362113024字节。原权限/时间、独立恢复
脚本及一次实际恢复/再共享记录见上述输入组合证据；原路径仍可读取，
若需写入须先独立恢复。当前host/runtime/default、模型和reference未改。

Live阶段另可逆压缩37份本任务退役交付快照，释放347293682字节。
原SHA、路径、权限与恢复脚本保留于
`.q4t-work/serve-live-20261121/retired-previous-01/`；当前默认/服务包未动。

视觉行最大值候选及原环境阻断继续保留，见
[视觉补验结果](VISION_ROWMAX_COMPLETION_RESULT_2026-11-07.md)；
本阶段没有追加该候选GPU测试。原研究/回退包与依赖原路径的工作区
继续保留。

早先整理的STATUS原件保存在[2026-11-20历史快照](HISTORY_STATUS_2026-11-20.md)，
SHA256：`af362f9c1ec4e7df087d577c7922e0414e606bf94e57fd6e7f16c1c0aa9c1d85`。更早原件见[2026-11-12快照](HISTORY_STATUS_2026-11-12.md)。
本次精简前的完整状态保留在提交`aafa4a63e`；历史版本与“待合入”
描述不代表当前默认。开发日志按日期追加，见
[当日日志](log/2026-11-21.md)。
