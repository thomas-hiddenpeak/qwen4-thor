# 当前状态（2026-11-20）

Serve总目标仍为 **active**。按[完整计划](SERVE_MASTER_PLAN_2026-11-08.md)
和[逐项任务表](serve-master-goals-v2.json)完成全部33项任务；阶段验证、
发行和PR合入都不是总任务终点。非性能改动只做受影响行为的针对性
验证与必要业务短验，长稳实跑与已延期听感由用户负责。

## 正在推进

[PR #54已实际合入main](evidence/serve-pr54-merged-20261120/README.md)，
合并提交`105e5f0df45f823e85d8201abe06bf8518270d11`。已验收提交
`c4ed24574`的两份最终CI全部通过，主线祖先关系已核对。
[rev376原生语音输出](evidence/serve-realtime-speech-delivery-20261120/README.md)
已可回退交付；服务保持STOPPED。新双向输入工作没有混入PR #54。

正在按[双向音频计划](SERVE_REALTIME_DUPLEX_2026-11-20.md)连接同一个
conversation的音频输入、原生ASR/VAD、手动提交、打断和自动回答。
当前已完成下列开发与CPU组件检查；真实双向协议已有原始证据，
空闲已修复并完成针对短验，受影响主轻/恢复与交付仍待完成：

- [有效输入设置](evidence/serve-realtime-duplex-settings-cpu-20261120/README.md)：
  15个不同用例/17次执行，独立回答与打断开关、公开转写开关、输入
  格式和空闲超时；原重复字段FAIL保留并修复。
- [同一会话输入历史](evidence/serve-realtime-duplex-history-cpu-20261120/README.md)：
  30个不同用例/38次执行，原生票据/预留、ASR完成与失败、隐私转写、
  删除后的迟到结果和执行快照；原8项缺省字段FAIL保留并修复。
- [处理器配置事务](evidence/serve-realtime-duplex-configure-cpu-20261120/README.md)：
  5新+10相关用例，旧音频和借用未清理时拒绝改管线，只有一次会话
  设置确认，失败/未知验证不提交；独立转写入口复用同一路径。
- [提交前预留与上下文冻结](evidence/serve-realtime-duplex-capture-cpu-20261120/README.md)：
  12新+30相关用例；提交前的流式输入不公开，待ASR请求冻结确切
  历史和generation，失败/删除/复用不能偷换输入。
- [原生输入生命周期](evidence/serve-realtime-duplex-owner-cpu-20261120/README.md)：
  11新+17相关用例；手动/连续/语义输入同一owner，通知先于执行，
  准入等待沿用原期限，真实借用保留到join，未知结果不能报成功。

- [同一会话输入事件组合](evidence/serve-realtime-duplex-composition-cpu-20261120/README.md)：
  33个不同用例/34次执行；GA事件、隐私转写、真实PCM检索、精确删除、
  commit时冻结上下文和输入失败隔离。原故障夹具与来源门禁失败保留，
  仅补受影响检查；实际Chat入口仍待接线。

- [实际会话输入与轮次owner](evidence/serve-realtime-duplex-session-cpu-20261120/README.md)：
  53个不同用例/61次执行；待ASR回答保留原请求/期限/上下文，VAD
  打断与自动回复、同会话准入交接、借用故障后继续文字已通过CPU。
  修复response.create引用被SDK旧外层拒绝的真实遗漏；原8失败保留。

- [原生输入后端构建与CPU](evidence/serve-realtime-duplex-backend-cpu-20261120/README.md)：
  16个不同用例/18次执行，14项按精确源码复用，原2夹具失败保留。
  ChatServer接入共享原生ASR/VAD及组合内存预算；实际q4t构建零警告。

十四组均完成相应来源反例检查，构建零警告，新增GPU为0。累计按原始
测试名去重为229个不同用例/381次执行，保留20次原CPU失败与
1次来源门禁失败；之前前五组92的误计已纠正为91，原记录不改。
host为`9ccf3773c0c3002188b78661526b1410c4e10db7dee300892e557d12a81e5c25`，
已构建q4t候选为`9376abee878e9181bf9fdf31cf68f1efee7adbea8b3543369fe6aedab3a0182c`，
保存在`.q4t-work/serve-realtime-duplex-20261120/idle-fact-runtime-01/q4t`。
两者都不是当前默认。实际双向业务已执行，最新结果见下方独立记录。
继续统一WSS/
ASR/TTS短验、受影响恢复与可回退交付。
[同会话本地输入恢复CPU](evidence/serve-realtime-duplex-recovery-cpu-20261120/README.md)
18项（6新+12相关）/0.637秒和20来源反例通过，host构建55.597秒零警告。
实际join后恢复同一管线，沿用历史/音频预算、时钟与generation；5秒
冷却、固定60秒窗口最多2次，不重放旧输入、不重载模型。设备健康
保护的ChatServer改动待下一次runtime构建；真实双向业务仍未验证。

[空闲触发CPU](evidence/serve-realtime-duplex-idle-cpu-20261120/README.md)
26项（10新+16相关）/0.673秒和20来源反例通过，构建59.277秒零警告。
回答终态加实际播放时长计时，保留实际接收音频交给ASR；零样本不
伪造波形/ASR成功，原生空闲提示只在执行快照。清除/语音/故障/取消
解除计时，旁路不重置。继续直接混合输入、runtime构建和统一业务短验。

[混合音频历史CPU](evidence/serve-realtime-duplex-mixed-history-cpu-20261120/README.md)
18项（6新+12相关）/0.515秒和20来源反例通过，构建43.259秒零警告。
按原内容索引回填真实转写，部分完成保留整项预算，隐私过滤覆盖
全部音频段，失败和迟到旧generation不能被新成功覆盖。直接音频
执行尚未接通，继续有限音频处理、队列及item/response.create入口。

[有限音频队列CPU](evidence/serve-realtime-duplex-finite-input-cpu-20261120/README.md)
22项（10新+12相关）/0.685秒和20来源反例通过，构建38.455秒零警告。
独立波形复用既有降噪及共享预算，不混入append ring；32项队列、
一小时总样本寿命由全部输入共用，恢复不退还。实际混合item/
response.create入口仍待组合，不能把组件结论当作真实业务通过。

[直接混合音频入口CPU](evidence/serve-realtime-duplex-mixed-entry-cpu-20261120/README.md)
36项（18新+18相关）/0.748秒和20来源反例通过，构建49.680秒零警告。
item.create逐段真实处理/检索/删除，response.create临时输入按请求
清理、保留默认历史；用户transcript独立限额、仅公开参考，模型使用
实际处理结果。空波形不伪造ASR或空闲提示。继续runtime构建与统一
WSS/ASR/TTS、受影响恢复/交付，全部master继续；0GPU、默认rev376保持。

[完整运行候选与短验驱动就绪](evidence/serve-realtime-duplex-runtime-ready-20261120/README.md)：
q4t构建147.168秒、零警告，10项驱动CPU反例与SDK语法0.262秒通过。
真实短验固定同一租约最多1模型/2WSS/9回答/7音频输入/2Chat/1200秒，
覆盖ASR→回答→TTS→ASR、真实工具、VAD打断及空闲。下一步执行该组
真实业务并记录所有失败，然后受影响恢复/交付。该准备阶段0GPU；后续实际执行见下条。

[真实双向短验ENV_ABORT](evidence/serve-realtime-duplex-env-abort-20261120/README.md)：
1模型/90.485秒，59次NVML正常后init查询未回收，严格验证监控中止。
原启动漏传锁参数的0模型失败也保留。普通Chat回答42；SDK最终事件
未落盘，不能认定双向业务/质量通过。模型退出-9，所属进程/端口/锁
已核实清理，默认rev376不变。先补验收驱动的持续事件落盘及中断CPU
检查，再补缺项；受影响恢复和交付仍待完成，总目标保持active。

[中断取证修复CPU](evidence/serve-realtime-duplex-interrupt-evidence-cpu-20261120/README.md)：
2 Python+4 Node及SDK语法/1.415秒通过，包含实际SIGKILL和超时子进程。
事件与stdout/stderr持续落盘，原10项按函数身份复用；未追加GPU。
候选运行时不变，原ENV_ABORT保留。NVML根因仍未确定；按工具缺陷
修复准备仅一轮独立有界补验，严格互斥/监控保持，不盲目重启循环。

[双向业务原记录](evidence/serve-realtime-duplex-business-20261120/README.md)：
1模型/2WSS/9回答/7音频/2Chat，88.259秒，82次NVML正常，模型退出0，
资源清理、默认rev376保持。混合/临时输入、语音往返、实际工具rev376、
VAD自动回答/打断及结算通过协议核对；13项质量12通过，空闲READY
回答失败，整组原FAIL保留。服务端空闲cue额外注入“邀请用户继续”，
先离线核对并移除响应策略注入；不把该推测当唯一根因。针对性修复/
短验之后继续主轻、Guardian恢复和可回退交付，全部master保持active。


[空闲事实修复CPU/构建](evidence/serve-realtime-idle-fact-cpu-20261120/README.md)：
8 C++/4 Python/20来源反例及SDK语法0.773秒通过，host28.296秒、runtime
22.537秒均零警告。实际记录经过生产规划器重建，新旧只差空闲cue；
应用指令、历史、采样保持。候选9376abee…仅供后续有界短验，不是
默认。原失败保留；最多1模型/1WSS/2回答/1音频/6Chat/900秒，新旧
seed41/42对照及真实空闲；继续主轻、恢复、交付和全部master。

[空闲实际补验通过](evidence/serve-realtime-idle-http-20261120/README.md)：
1模型/1WSS/2回答/1音频/6Chat，53.632秒，52次NVML正常、模型退出0。
实际空闲READY、ASR/回答、3次准入/363 tokens及清理通过。seed41/42
的新旧请求均正确，不能将原失败唯一归因于被删除指令；原FAIL保持。
默认rev376未替换；继续双向能力主轻资格、恢复/回退保留及可回退交付。


[双向主轻/恢复合同CPU](evidence/serve-realtime-duplex-profile-cpu-20261120/README.md)：
16 Python通过；5个新模块漏登记的来源FAIL保留，补登记后20反例/
0.465秒通过，16例按未变源码复用。v20要求真实双向与组合3.5GiB预算，
自动恢复不能丢输入；8MiB证据上限保持。24改动/4新增/956未变来源
已核对，候选9376abee…不变。接着同租约2模型/10WSS/42原生工作/
6入口/1200秒的主轻短验，再Guardian恢复与交付；当前新增GPU0。

[主档原质量失败与补验准备](evidence/serve-realtime-duplex-profile-quality-20261120/README.md)：
主档1模型/5WSS/21原生工作/109.240秒，103NVML正常、清理通过，但
mixed重复内容、vad_auto译成英文两条原答案FAIL，轻量未跑。生产
提示重建未发现转写/指令丢失；仅验收客户端补明确复述说明，服务
数值/默认采样不变。21Python通过；修正来源登记与CI续行后20来源
反例通过。接着最多2模型/34工作/7WSS/1200秒固定补验，原失败保留。

## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev376；服务包`68345e1781eebda59d530bcad2d235e9fe08365f64929a0caa97f0cecd6a84dd` |
| 引擎 | `b156d976be1ee7009784ac6b98b84b9c3fa3adc76d0ba619c93555fca5c1530e` |
| 协议 | `c0571cd5ee593142b679e016862b492187cb5425132ec9bb6aa60d2b759aaea4` |
| 引擎发布记录 | `8829463b2ba83459b7a094c09ba5903f44f36cbb6b9dadcffb9573fd371c1a1b` |
| 原生入口 | `4dff0f66eb25c570d6d89470aecfd8670768e3b1b149932f2b24d0e5a82de826` |
| 状态 | STOPPED；本阶段没有启动生产服务 |
| 客户限制 | alpha RPM30/TPM6000/输出3457；beta RPM1/TPM6000/输出256；保留动态值与已消费预算 |
| 主轻资格 | `d28ba7ac49169a0a4bd411ecd7e4f99cebe217b18cf7fd9e1c920e7c531f8b4f` |
| 保留控制包 | `77b25732b6a04471a080fadb73fdc9be2de7f33be34ec3b844a6b151fe3a9d30`；同一已修复引擎和语音能力，只切换已验旧控制器 |

发行完成19条原生管理命令，包括实际升级、STOPPED整根回退和再前进。
[恢复与取消修复证据](evidence/serve-realtime-speech-recovery-20261120/README.md)
包含全部失败尝试，共9模型/22生成/11连接/1探测/604.676秒，570次
NVML正常并清理。原TLS取消误计错误的失败保持，修复另有真实补验；
未改变组件按精确来源复用。候选→保留包故障恢复已验，反向登记和
STOPPED回退已验，没有另做反向运行故障注入。

当前Realtime已交付独立转写会话、语义VAD、文字/函数工具对话、
持久Prompt和原生语音输出（PCM/G.711、音色/语速、历史、播放截断、
取消结算）。同一conversation双向输入、MCP/追踪及Live仍是实现任务，
不能宣称完整Realtime或完整OpenAI兼容已经完成。原答案/模型质量、
数值边界、声纹与驱动失败保持各自记录，不追认旧失败为成功。

## 全部任务进展与剩余缺口

详细内容与原始证据以[任务表](serve-master-goals-v2.json)为准。

| 范围 | 当前状态 |
|---|---|
| G00 | 平台管理API范围仍待用户决定，不阻塞已确定的实现 |
| A03—A06/A08 | 采样、工具控制、结构化输出、Completions/Models与持久会话已交付 |
| A01/A02/A07/A09—A12 | 已有大量实际业务交付；剩余字段/媒体/内建后端、双向Realtime及完整客户端组合继续 |
| N01—N07 | 原生接入、TLS/CORS/Key/配额、HTTP、运维和发行已交付；nginx/systemd为可选集成 |
| R01—R06/R10 | 辅助故障隔离、自监护和已验恢复已交付；新增能力的恢复组合继续 |
| R07—R09 | 冷准入、物理DRAM边界及压力/宿主组合仍需完成；未获维护交接不干预其他服务 |
| Q01 | 项目资料查询真实闭环已交付，新增后端/客户端/故障组合继续 |
| Q02/Q03 | rev376已可回退交付并合main；后续实现、最终能力矩阵和长稳交接继续 |

辅助依赖失能不能连带停止健康核心业务；监控恢复不自动重启模型，
真正设备故障仍保留保护。真实云账户验证依赖部署方配置，不能把
本地合同测试写成真实账户验收。长稳工具/交接仍属本任务，实际长跑
由用户开展。

## 工作区、证据与保留候选

有效工作分支仍为`codex/serve-resilience-cd`，包含尚未进入main的双向
输入开发，因此继续保留。仅在独有提交、未提交内容、活动PR和证据
路径均核对后整理重复分支，不删除其他任务内容或已发表回退包。

为当前构建恢复了591个host对象，未覆盖后续更新的runtime库；旧
CPU夹具和退役构建库的可逆归档、SHA和恢复脚本保留在
`.q4t-work/serve-realtime-duplex-20261120/`。当前模型与reference目录
保持只读，默认包和动态状态未随开发替换。
磁盘余量不足时另将67个退役CPU测试程序精确压缩归档，释放
188854224字节；当前host测试、runtime和发行包保持，原路径及
逐字节恢复方法见该目录的`retired-host-tests-03/plan.json`与`restore.py`。

另将14个2026-10-31及以前历史包的同字节head副本转为只读共享inode，
保留全部原路径和SHA，释放4362113024字节。原权限/时间、独立恢复
脚本及一次实际恢复/再共享记录见上述输入组合证据；原路径仍可读取，
若需写入须先独立恢复。当前host/runtime/default、模型和reference未改。

视觉行最大值候选及原环境阻断继续保留，见
[视觉补验结果](VISION_ROWMAX_COMPLETION_RESULT_2026-11-07.md)；
本阶段没有追加该候选GPU测试。原研究/回退包与依赖原路径的工作区
继续保留。

本次整理前STATUS原件完整保存在[2026-11-20历史快照](HISTORY_STATUS_2026-11-20.md)，
SHA256：`af362f9c1ec4e7df087d577c7922e0414e606bf94e57fd6e7f16c1c0aa9c1d85`。更早原件见[2026-11-12快照](HISTORY_STATUS_2026-11-12.md)。
历史版本与“待合入”描述不代表当前默认；开发日志按日期追加，见
[当日日志](log/2026-11-20.md)。
