# Interactive 生命周期失败法证（2026-10-26）

本次正确使用 `start → launch → stop`，仍在 Nsight 内部的 `RangeCollectionState::Stop` 被拒绝。已定位拒绝的状态、函数和错误，但没有证明一个可在本地确定修复的选项或控制代码错误。阶段保持原始 `STOPPED`、postflight `ENV_ABORT`：只有 direct-interactive 启动，wrapper 和全部模型对照未执行，0 HTTP、0 模型加载。没有追加预算、导出、重试或扫描参数；原运行时 NO_GO 不变。

本报告仅读取既存控制收据、server/NVLOG、原始 seal 与保存的官方资料。34 个 run seal 条目全部匹配。完整文件 SHA 见同目录 `lifecycle-forensics.json`；NVLOG 的认证资料未复制到报告。

## 明确观察到的顺序

1. `start` 返回 0，配置 `capture-range=cudaProfilerApi`、`capture-range-end=none`、`flush-on-cudaprofilerstop=true`、`stop-on-exit=true`。代理状态真实经过 `Idle → Configured → Started`，随后 launch 后进入 `StartRange → RangeCollection`。这不是上轮 `profile` 命令创建的会话。
2. launch 使用 `trace=cuda,nvtx`。实际 fixture 为 PID 3406149 / start_ticks 113655876；其父链经 nsys-launcher 3406148、launch CLI 3406128 到稳定 owner 3406102，已保存所有权收据。32 条 host step 输出与 ready 收据成功；ready 中 ProfilerStart、ProfilerStop、最终同步均返回 0。
3. 目标日志明确 `flush on cudaProfilerStop = true`，并记录 ProfilerStop 的同步/flush 入口（NVLOG 1108、1181、3457–3458 行）。start CLI 曾提示当时没有启用 CUDA tracing，因此 flush 选项不会生效；该启动警告不能覆盖 launch 后目标实际记录的配置与执行。
4. ready 后发送同名 `stop --keep=0`。客户端与代理 build ID 相容，连接及 Subscribe 成功，Stop 请求进入代理。NVLOG 4943–4949 行显示 `SessionStateImpl.cpp:483` 的 `QuadDDaemon::RangeCollectionState::Stop` 抛出 `LogicException`，信息是 `Range collection stop is not expected.`；CLI 返回 1。这排除了会话名找错、start 失败、命令解析失败和 stop 超时作为本次直接拒绝原因。
5. 失败路径对已绑定目标和 launcher 做强制清理。NVLOG 4972–4995 行记录 SIGTERM、目标被 signal 15 终止，继而从 `RangeCollection` 进入 `RangeGeneration`；5953 行进入 `RangeImportation`。后续 report-ready 和自动 `Shutdown` 均有记录（8663–8680 行）。
6. 随后的唯一 `shutdown --kill=none` 返回 1，原因是已经没有活动会话。它发生在上述自动关闭之后，是失败清理后的次生错误，保留在 cleanup_errors；不能改记为成功。

实际目标没有 `[fixture-exit]` 或独立正常退出收据。launch CLI 的 143 与被收养 nsys-launcher 3406148 的 −15 都不能替代实际 fixture 的退出码。局部 reaper 已记录其已知身份的回收和 subreaper 恢复；未观察到的 daemon 后代仍属于外层 guard 独立清理审计范围。

## 与公开合同的关系，以及需要纠正的判断

保存的 [NVIDIA 2026.1 User Guide](https://archive.docs.nvidia.com/nsight-systems/2026.1/UserGuide/index.html) 将 stop 定义为 interactive collection 的控制命令；3832–3838 行给出命名 start/launch/stop，3844–3859 行给出 cudaProfilerApi 的交互采集，并列出 profiler stop、手动 stop、进程退出等结束方式。本机 start help 又明确：`capture-range-end=none` 忽略 API capture-range 结束，默认值是 stop（32–52 行）。

这些资料分别支持所用命令及选项，但 guide 的 API 示例采用默认结束策略，没有明确演示本机 2026.1.3 中 **API range + end=none + 手动 stop** 的精确组合。本次原生日志表明该组合进入 `RangeCollection` 后拒绝 StopRequest。公开资料不揭示内部 StopReason 分派条件，尚不能判定它是未说明的组合限制、Nsight 实现缺陷，还是另一个内部配置条件。

必须修正上轮的因果表述：profile 与 interactive stop 混用确实与公开命令合同不符，但不能作为同一内部拒绝的充分解释。本次正确交互会话仍出现该拒绝。此前静态设计中“改成 interactive 后 end=none 一定能由手动 stop 闭合”的判断没有被验证，且本轮与该判断相反。

目前没有足够证据选择一个确定的本地修复。改成自动 capture-end=stop 会换回旧流程，旧目标完整性 warning 尚未解释；取消 range、改用普通交互 start/stop 又会改变采集窗口，不能当作同一合同的无害修补。本阶段不执行这些变化。若未来仍推进，应先向官方确认上述精确组合及拒绝状态的支持合同，而不是自动增加采集次数。

## 可保留与不可推出的内容

`trace.nsys-rep` 为 78,991 bytes，SHA256 `523303e836f4094a402e38c511cba9969c2741950f4e077f4fd9c0a4dbb081f9`。它在失败清理导致目标退出后才生成，不是 stop 成功的报告。本轮未导出 SQLite、未执行 GPU 活动双向覆盖或已知工作审计。32 个 host 输出不等于已证明 32 kernel/64 copy/32 memset/33 NVTX 全部被采集。

现有 NVLOG 可以证明目标 flush 配置与入口；没有据此得到底层 flush flags、producer Done、consumer 终态或 drop=0 的完整证明。未导出诊断表，不能把日志中未出现旧 warning 视为 warning 已消除，也不能主张真实事件缺失。NVLOG 与 monotonic 收据时钟只各自用于顺序核对，没有混用计算耗时。

既存监控摘要为 8/8 样本、424 次 source read、无 monitor failure、pending 触发 0；本次阶段失败来自采集控制和强制清理，不是监控采样失败。该事实不能让不完整的采集候选通过。工程出口保持负向封存，无新 GPU 性能结论、无部署。
