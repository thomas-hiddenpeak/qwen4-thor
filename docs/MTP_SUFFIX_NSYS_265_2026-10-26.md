# 固定 k3 suffix：Nsight 2026.5.1 单候选资格与 GPU 对照

2026-10-26，入口 `d897a1e6`，沿用 codex/suffix-k3。完整交付目标继续；
这是采集依赖资格与开发定位，不是运行时优化接受或部署。

## 事前冻结

唯一候选为已隔离的 Nsight Systems CLI
`2026.5.1.161-265138896106v0`，ARM64 包 SHA256
`087c08b5ddf3ed6f913748f8206d6bbdc225f2aa275ff33af4c487e8b319c8ab`。
使用包内绝对 target CLI，不改系统安装、PATH、驱动或 CUDA。来源及
选择依据见[依赖准备](MTP_SUFFIX_COLLECTOR_DEPENDENCY_2026-10-26.md)。

静态能力预算为 180 秒、7 次 version/help 调用，每次最多 20 秒，零重试、
零 session/采集/GPU。实际 1.467566 秒，7 次返回 0；1765 文件前后同身份。
CLI/help 仅证明语法和文档语义，不证明运行时采集、SQLite 或 CUPTI 完整。
原件位于 `.q4t-work/suffix-nsys-265-20261026/`。

此构建 UserGuide 明确 `--trace=cuda` 在支持的 Blackwell 上默认采用
硬件/HES trace，且可自动回退软件。这里固定保留 `cuda,nvtx`，实际 backend
尚未观测；不声称与旧工具同 backend，不追加 `cuda-sw` 试验。缺少旧私有
flush/Done 标记只意味着现有工具合同不可用，不能单凭这一点认定应用
事件丢失。三个 ELF 的 NEEDED/RPATH 已静态保存，实际加载库另依原始
采集记录判断；包内有某个 CUPTI 库不等于实际使用它。

执行代码、fixture、监控、接受门、运行时 f0ff/55363f18、数据集及请求顺序
保持。复用同依赖的 32 项 runner/interactive CPU 合同、86 项未变合同和
64 原生 CPU 子进程证据，均保留原来源/范围；新准备器只负责冻结、独立
审阅及输入核对，不声称旧测试覆盖新准备器或新 collector。

完整 GPU 阶段由一次共享独占 lease 持有，固定如下：

| 项目 | 固定预算与顺序 |
|---|---|
| 阶段墙钟 | 4800 秒，零重试/额外 warmup/重建/运行时候选 |
| 新控制 | direct-interactive → wrapper-interactive，合计 600 秒 |
| 单控制 | 300 秒，NVLOG 最多 64 MiB；完整 32 次已知工作 |
| 条件模型 | 子集 8K OFF → 8K ON → 200K ON → 200K OFF |
| 模型预算 | 4 启动、6 HTTP，每组最多 900 秒，每响应 256 token |
| 200K 前置 | 每启动先执行同一个不采集的 8K 请求，随后采集 200K |
| 冷态与缓存 | 沿用原 plan 的新服务启动/自然页缓存约定，不清系统缓存 |
| 停止 | 首次身份、环境、监控、控制、完整性、负控、HTTP 或清理失败 |

每个新控制均要求 32 kernel、32 H2D、32 D2H、32 非零 memset、33 NVTX、
输出校验、API/活动双向关联、实际目标退出及完整 session 清理。删除实际
launch/kernel 对和最后 NVTX 的两个副本负控必须拒绝。两控制的已知工作和
负控都必须通过，所选 wrapper 还须完整诊断/终态/memory 资格，才允许
条件模型；direct 的诊断完整保留，不替代 wrapper 的干净资格。旧普通
stop 两控制仅作历史局部证据，旧 ENV_ABORT 状态保持。

候选 wrapper 及每份模型 trace 的实际目标 warning、未知 severity/PID/
表列/语义、反向 memory 缺口均阻断；
保留仅限已绑定且无事件非 CUDA wrapper 的原精确例外。新 SQLite 仍需
实际工作闭合与 schema 核对；旧 6381 次无活动 memset 的语义问题不能被
版本变化或 fixture 通过豁免。失败不切换另一个版本、调整选项或宽松解析。

## 身份、复用与结论边界

准备器绑定父 plan `4ea7b258`、独立 audit `6fba3a33`、raw seal、父快照、
新包/符号链接/帮助及实际命令；旧大快照引用复用，新准备输入单独保存。
同身份的两 head 重复收益、质量和直接合同按已知范围继续复用：完整 head
重复代码/文本 +13.6774%/+14.4034%，子集共享 B 描述性 GM
+6.5335%/+6.7304%。不把 profile 时间混入正常吞吐或推导统计非劣。

本阶段只回答采集工具资格和条件 GPU 区间差异；当前运行时五档已经测齐
但保护项未通过的 NO_GO 不因工具通过而改变。默认 f356、sidecar、子集
产物保持。后续运行时改进只能来自具体可修证据，并另冻结适用 HTTP E2E
及保护项验收；通过后再依授权交付。

结果分别记录证据有效性、测量完整性、统计结论和工程决定。竞争或监控/
清理失败记 ENV_ABORT，仅清理本阶段身份；预算耗尽后不自动扩样。

## 实际结果与决定

准备器独立静态审阅后首执行通过，29.053079 秒，绑定 46457 个输入；
父 snapshot 原路径复用，新增 8 个准备/文档副本及 patch。唯一 GPU 阶段
8.052779951 秒，第一项 direct-interactive 的 start/launch 成功、fixture
host receipt 32 步通过，但 stop 返回 1。正确会话/build 的 daemon 在
`RangeCollectionState::Stop` / `SessionStateImpl.cpp:492` 拒绝 StopRequest。
随后自有目标强制清理，shutdown 返回 1（无活动会话）。独立 fixture 退出
状态为 UNKNOWN；launcher 的 143 和被收养 launcher 的 −15 不是目标退出码。

生成的报告 82478 字节、SHA `f3c50f87ac5e`，生成于失败清理之后；未导出
SQLite、未验 trace 完整性。wrapper 和四组模型全部未执行，实际 0 启动/
0 HTTP。完成的已知工作/负控资格为 0；host receipt 不能冒充 GPU 事件闭合。

新原始日志明确绑定目标 PID 3412250，加载隔离包
`libcupti-sbsa.so.13.4`，HES 启用返回 27 后回退软件；ProfilerStop 同步与
flush 返回 0。旧 2026.1.3 同样 HES 失败回退软件，两个 start warning 文件
同 SHA `adc7930`。因此不能归因于新版首次开启 HES，或仅凭 start 的
“CUDA tracing 未启用”提示认定目标 flush 被关闭。接口范围配置、目标
flush 与内部状态拒绝都已保存，仍没有证明具体可修的本地原因。该数值
日志不证明警告误报、实际丢事件或子集下降已修。

监控 9/9 完整，保留 1 次同身份退出窗口缺 io，pending 分支本轮未触发；
不能继承旧 CPU 紧循环 15 次 pending 作为本轮覆盖。独立审计一次
9.374468 秒，46457 冻结输入、21297 父 snapshot、9 个 overlay 文件、34
raw 和197模型元数据一致。7 个记录的自有身份均 ABSENT，记录 PGID 无
残留，lease 已释放；失败屏障仅代表 step PGID，不声称一次轮询排除
所有短于采样周期的干扰。原数据不变，文档收尾变更发生在审计之后。

| 字段 | 本阶段结论 |
|---|---|
| 证据有效性 | 原始证据身份审计 PASS；阶段 ENV_ABORT，不能充当正常性能样本 |
| 测量完整性 | 不完整；尝试首控制，wrapper/6 HTTP/4 模型组未执行 |
| 统计结论 | 本阶段无性能统计结论；不更新既有两head重复收益或原五档结论 |
| 工程决定 | NOT_ACCEPTED_TOOL_VALIDATION_ABORT；保留默认和原运行时 NO_GO |

计划 SHA `201619eb2a52`；raw seal `7fb1daa55e5a`；独立 audit
`f2f9e9146f94`。原件位于同一工作目录的 `qualification-v1/`、`audit-v1/`；
公开摘要见[阶段证据](evidence/mtp-suffix-nsys265-20261026/summary.json)。
新工具及失败证据增量归档，旧独立归档和原路径是明确外部依赖，不重复
复制 21K venv 或把增量包称为自包含回退包。没有安装、部署或健康 HTTP。
完整交付目标继续保持未完成；后续需要新的具体合同/修复证据，本阶段
不再追加采集、其他版本或选项。

增量归档已完成：101 条目、254501440 字节，独立 inode 与前后 SHA /
最终回读通过；manifest `443481156a01`、seal `7b58904be3e2`。明确外部
锚点为上一归档 manifest `e98dfe625823`、seal `50d40526d7a6` 及原路径。
封存后的文档仅补充本闭合记录，阶段输入的原文保留在小 overlay 中。
[封存收据](evidence/mtp-suffix-nsys265-20261026/archive-result.json)。
