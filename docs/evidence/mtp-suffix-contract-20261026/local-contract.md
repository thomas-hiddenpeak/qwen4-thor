# 本地 Nsight API-range/manual-stop 合同复核

2026-10-26；只读、离线、限定 8 个相关本地来源。没有启动进程采集、Nsight 会话、GPU、HTTP、测试、导出或硬件采样，没有修改旧证据。不是重新进行阶段完整审计。

**结论：公开资料笼统支持 API-range 的 interactive manual stop，但没有明确覆盖本机 2026.1.3 的 `cudaProfilerApi + capture-range-end=none + ready 后手动 stop` 组合。现有原生日志明确证明该组合被内部 `RangeCollectionState::Stop` 拒绝。本次本地资料中没有找到将这一组合列为已知限制的明确条目，也没有证据确定一个本地选项修复。**

## 证明到哪一步

已保存的 start 命令明确使用 `--capture-range=cudaProfilerApi --capture-range-end=none --flush-on-cudaprofilerstop=true --stop-on-exit=true`。既存 NVLOG 的状态为 `Idle → Configured → Started → StartRange → RangeCollection`（181、185、1018、2680 行），目标只记录一次 ProfilerStart 和一次 ProfilerStop（1390、3457 行）。源码 fixture.cu 第 100、121 行也只调用一次这两个 API，32 次工作循环位于它们之间。

Stop 客户端与代理 build 相容，成功连接后发送 StopRequest（4919、4938、4942 行），代理在 `SessionStateImpl.cpp:483` 的 `RangeCollectionState::Stop` 抛出 `Range collection stop is not expected.`（4943–4946 行）。因此不是“没有创建 interactive 会话”、找错 session 名、命令解析失败或控制请求根本未送达。内部状态拒绝已证实；具体 StopReason 分派条件和产品支持边界没有公开实现可核对。

目标日志 1108 行记录 range-end action=0、flush=true；3457–3458 行记录 ProfilerStop 同步/flush 入口。`end=none` 的公开含义只是忽略 capture-range 结束，不能由此额外推出内部 manual Stop 必然可用。失败清理使目标退出后，状态才变成 `RangeGeneration → RangeImportation → Shutdown`（4993、5953、8678 行）；8781 行的“没有活动会话”是之后 shutdown 的次生结果。这里不重算旧 seal、监控或活动数量。

## 公开合同与本机帮助的差异

1. 保存的官方 2026.1 guide 文本 190–192 行将 stop 归于 interactive；3832–3838 行给出命名 start/launch/stop；3844–3859 行又把手动 stop 列为 cudaProfilerApi 交互采集的结束方式。安装包 UserGuide HTML 3558–3567 行有同样示例。
2. 本机 start help 17–30 行要求 API range 启用 CUDA tracing；32–52 行列出 end 的 `none/stop/repeat[:N][:mode]`，默认 stop，none 忽略 capture-range 结束。stop help 18–21 行只要求合法 session 名或 ID，没有陈述 range-state 限制。
3. 安装包 UserGuide HTML 2572–2578 行的对应选项表仍列 `stop-shutdown` 等值并写默认 stop-shutdown，与本机 help 不同。HTML 3306–3315 行甚至在 `nsys profile -c nvtx` 示例后写 interactive/manual stop。由这些文档组合，既不能证明“profile + stop 一概不兼容”，也不能保证“换成 start/launch 后，end=none 一定能手动结束”。此前将 profile 混用当作该内部拒绝的充分原因，需要纠正。
4. UserGuide HTML 3570 行的已知问题涉及大量短 ProfilerStart/Stop 对；本 fixture 只有一对，不能套用。ReleaseNotes HTML 的多进程正常退出/flush（562 行）、launch→start→stop 的 LinuxPerf 线程发现窗口（567–575 行）、多 range CUDA 内存图限制（657 行）也没有说明本次 RangeCollectionState/StopRequest 拒绝。没有在本次限定阅读中发现 end=none 的明确已知限制；这不等于证明所有版本或全部官方渠道都没有记录。

上述 guide 与 help 的真实差异说明应以本机 help 描述可解析选项，同时把精确组合的运行结果作为实现事实。不能用文档示例掩盖已观察到的拒绝，也不能把内部报错自动升级为已证实的产品 bug。

## 因果修正与尚待核验的线索

**已证明：** 这一 installed build 在上述固定配置和控制时序进入 RangeCollection 后拒绝 manual Stop；原错误不是仅因使用了 profile 命令。**推断：** 余下问题集中于 API-range 状态对 manual Stop 的处理，而非请求传输。**未知：** 是未说明的组合限制、实现缺陷，还是另一个内部配置条件；能否通过某个工具版本或控制方式修复；该拒绝与旧 CUDA/NVTX 完整性 warning 是否同因。

根代理在独立官方网络检索中提供了一个新的工具依赖线索：[当前 ReleaseNotes](https://docs.nvidia.com/nsight-systems/ReleaseNotes/index.html) 的 2026.5.1 条目涉及 cudaProfilerStop 结束时的多进程 CUPTI flush 改进；官方论坛问题 350407 的 DTSP-21073 讨论也涉及仅调用方 flush，其他进程丢事件及 manual-stop workaround。此网络证据由根保存，本报告没有额外抓取或独立验证其全文。它可以支持后续静态核对版本适用范围，**不能证明本次仅一个执行 CUDA 的 fixture 的 StopRequest 拒绝已修复，也不能自动说明旧目标 warning 都属该缺陷**。wrapper 本身没有 CUDA 活动与“多个 CUDA 生产者未 flush”是不同合同。

可继续而无需新采集的工作是：把精确配置、状态拒绝、文档差异和版本修复条目整理成可向官方核对的技术问题，要求确认修复是否覆盖 API-range end=none 的 manual Stop，以及所指的多进程/生产者范围。当前没有从这些证据直接选出已验证的本地运行时候选或采集修复，不自动增加 GPU 预算，不做选项扫描，不改变原 ENV_ABORT、完整性门和运行时 NO_GO。

## 来源身份与行号

行号均为对应原文件的一基行号；HTML 行号为源 HTML 行号，不是重排后的文本。只引用不含认证信息的 NVLOG 行。

| 来源 | SHA256 | 本次主要行号 |
|---|---|---|
| `.q4t-work/suffix-delivery-20261026/references/nsys-2026.1-guide.text.txt` | `c1802b141d5aeb2b87774469dc1fb0d6d37d0b2690f20cd195561ca619a3d6f2` | 190–192、2746–2774、3832–3872 |
| 同目录 `nsys-start-help.txt` | `ba19a842127ded3d06222cd02a9029fe554c831105ad03e24ba10fd261246eb5` | 17–52 |
| 同目录 `nsys-stop-help.txt` | `81dba69c56508cef65c5e4727b81443c6f522f82f3f8fb79a9ea16ba01923b05` | 13–21 |
| `/opt/nvidia/nsight-systems/2026.1.3/documentation/UserGuide/index.html` | `f600337bed7d55e59630f9089459a36bceece0e4a760f6bbc65744276e718cfe` | 500–530、2572–2578、3306–3315、3558–3570 |
| `/opt/nvidia/nsight-systems/2026.1.3/documentation/ReleaseNotes/index.html` | `ae3681880a2e570fdd1704c8bf87411d369f209a21b2bcc5a7862193015547e3` | 562、567–575、614–630、657 |
| `.q4t-work/suffix-delivery-20261026/diagnostic-v1/run/fixture-controls/direct-interactive/nvlog.log` | `5b10207b04d32a88e1c5391eb6b0adb56c0b73be4b51d0b013b5dec2b46bf130` | 181、185、1018、1108、1390、2680、3457–3458、4919–4946、4993、5953、8678–8781 |
| 同 fixture 目录 `interactive-start-command.json` | `2b4921d00ff8b137e075182439cdd4d1bf78896ada8a08d6f834cbb170ac3b91` | 命令数组 |
| `tools/mtp_suffix_nsys/fixture.cu` | `01857e095c307c68d66fe7bd5203b4c310a1cfce2d3cde531e1b5454828002fb` | 100–121、151–176 |

官方保存来源地址为 [2026.1 User Guide](https://archive.docs.nvidia.com/nsight-systems/2026.1/UserGuide/index.html)；安装包文件作为本机官方分发资料核对，不声称本轮重新下载。没有读取或公开会话密钥。
