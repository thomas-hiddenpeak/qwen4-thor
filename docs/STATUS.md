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
当前已完成下列开发与CPU组件检查，**实际HTTP后端已接线，真实业务尚未验收**：

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

十组均完成相应来源反例检查，构建零警告，新增GPU为0。累计按原始
测试名去重为190个不同用例/297次执行，保留20次原CPU失败与
1次来源门禁失败；之前前五组92的误计已纠正为91，原记录不改。
host为`d0498c558130b1a0a8cde729dac127e3f514b25985ada896a361eab27ce09442`，
已构建q4t候选为`1f01ec621cd7a79ca037e3f5308325c0b3e10d9f00c66da04896ea03f6c5e577`，
保存在`.q4t-work/serve-realtime-duplex-20261120/backend-runtime-01/q4t`。
两者都不是当前默认。实际HTTP后端已接线，**尚未执行真实双向业务**。
继续直接混合音频和必要runtime构建，然后统一WSS/
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
