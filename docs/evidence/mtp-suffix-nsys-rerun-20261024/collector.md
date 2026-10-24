# Collector rerun 只读法证（ENV_ABORT 保留）

阶段 diagnostic-v2 运行 10.026 秒，0 模型加载、0 HTTP。前两组完成，第三组的外部停止命令失败，第四组未运行。postflight 为 ENV_ABORT，外层锁已释放；本报告不恢复阶段完整性、不产生 GPU 性能结论，也不放行候选。

## 已完成的两组

| 变体 | 实际目标 PID | kernel / copy / memset / NVTX | 删除负控 | 实际目标警告 | wrapper 警告 |
|---|---:|---|---|---|---|
| direct-stop | 3195549 | 32 / 64 / 32 / 33 | 两项均拒绝 | CUDA + NVTX | 无 wrapper |
| wrapper-stop | 3195642 | 32 / 64 / 32 / 33 | 两项均拒绝 | CUDA + NVTX | PID 3195632 独立四条 |

这些是独立 32-step 输出账本、API 与活动关联、方向/字节数、末同步和既存负控的局部合同结果。非零 memset 512/16384 字节各 16 次完整。此次仅只读核对既有审核及 SQLite 计数，没有再运行负控。两目标及 launcher 正常退出；wrapper 的所有现有 CUPTI/NVTX 表计数均为 0，其四条空进程警告与目标两条警告单独保留。直接采集也有目标警告，排除了“只有空 wrapper 导致目标警告”这一充分解释。

两组目标 NVLOG 都记录 flush flags `[0,0,0,1,1,1]`，CUDA `ReportCUDADone: DONE` 和 `NVTXFinish`。它们证明 producer 执行了这些路径，不能证明 consumer 已确认终态，也不能据此将警告判成误报。CUDA Done 不是 NVTX 完整性的替代证据。显式数值 drop counter 未提供，仍是 unknown；日志未匹配 drop 不能填 0。两组的 CUPTI produced 304 与 importer collected 292 未证明同属一个计数域，不能相减宣称掉了 12 条。

## 第三组 StopRequest 的具体错误

实际 argv 是 `nsys profile --capture-range=cudaProfilerApi --capture-range-end=none --flush-on-cudaprofilerstop=true --session-new=q4t-suffix-3195519-direct-external`。目标 PID 3195723 已输出 32 个成功 step 与 ready；ready 的 profiler start/stop、末同步均返回 0。随后驱动向同名 session 发 `nsys stop`，约 100.789 ms 后返回 1，stderr 为 `The request was not completed properly: StopRequest.`。

NVLOG 给出更精确的顺序（均为同一日志本地时钟；不与 SQLite 或 monotonic 时间轴直接相减）：

1. 10:23:34.507 创建同名 session；10:23:35.025 进入 `RangeCollection`。
2. 10:23:35.033 目标调用 ProfilerStop，日志明确同步/flush，并出现 flag 0；range-end action=0，保持采集。
3. 10:23:35.144 新客户端 build ID 相同、认证 secret 相符；10:23:35.145 成功 Subscribe。
4. 同在 10:23:35.145，`SessionStateImpl.cpp:483` 的 `RangeCollectionState::Stop` 抛 `LogicException`：**`Range collection stop is not expected.`**（nvlog.log 行 5868–5871）。
5. 10:23:35.185 才出现 SIGTERM；10:23:35.422 记录目标被 signal 15 终止，随后 `RangeCollection → RangeGeneration`。报告生成是失败清理之后的事实，不是外部 stop 成功的证据。

此顺序证明请求已到达正确的 agent/session，且状态拒绝在强制清理之前。不是监控 EACCES、session 名称不一致、客户端版本/认证错误所导致的此次停止。第三组只有 flag 0，没有目标 CUDA Done/NVTXFinish；这与失败后终止相符，但不独立证明缺失原因。保留既有 `.nsys-rep`，未导出第三 SQLite，也没有第三组 trace 完整性/负控结果。

## 设计错误与剩余未知

[NVIDIA 2026.1 官方指南](https://archive.docs.nvidia.com/nsight-systems/2026.1/UserGuide/index.html) 将 profile 描述为无需且不接受后续输入的完整采集命令；stop 的合同是停止 interactive mode 启动的 collection。该指南的命名会话例子用 start/launch/stop，ProfilerApi 的交互例子也是 start 后 launch。引用来自原先保存的官方 HTML/text，身份记录在 JSON；本次网页工具重新打开失败，没有把未读的线上页面当新证据。

**强推断：本轮把 profile 模式与 interactive stop 混用，导致明确的 RangeCollection Stop 状态拒绝。** 公开文档合同与实际异常一致；专有实现条件未公开，因此不声称已读到全部内部判断。此前把“profile 可命名”进一步推为“兼容外部 stop”的设计假设需要纠正。名字匹配只证明路由正确，不证明命令模式兼容。本轮没有真正完成外部停止处理，不能据此判断正确的 interactive stop 机制无效，更不能判断其能消除目标 warning。

未来最小修复线索仅是工具生命周期：若另获授权，改成文档支持的 interactive start/launch/stop，先静态确认选项分别归属并用 CPU 合同固定命令顺序、精确 session 与所有权，保证目标工作前完成采集准备、目标退出与控制命令分别收据。不能只增加 sleep、重复 stop 或放宽 warning。其已知工作采集是否完整仍需之后单独冻结的验证；本次没有实施或追加采集。

## 环境与有效性边界

telemetry 为 11 attempts / 11 flushed，无失败、pending admitted/resolved/active 均 0；没有自然触发新 pending 分支。独立原生 CPU proof 的 15 次 pending 不能写成此次 1 Hz GPU 阶段的分支覆盖。失败收尾 fresh barrier 已闭合，但其明确范围仅为 owned step PGID 清理后；detached descendants 的收尾属于外层 guard，不混称同一个屏障。外层最后记录 lease_released/COMMAND_FAILED。轮询不能排除短于周期的干扰。

证据是 **局部法证可用 / 整阶段 ENV_ABORT**；测量不完整；无模型/HTTP 统计或 GPU 收益结论；工程上没有 candidate pass，不部署、不自动扩样。模型重复收益只能按原身份与范围复用旧有效证据。
