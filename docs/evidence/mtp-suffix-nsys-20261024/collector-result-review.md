# Nsight collector 单次失败法证

阶段保持 **ENV_ABORT**，guard 为 COMMAND_FAILED / child 1；仅 direct-stop 产生报告和 SQLite，另三组未运行，0 模型启动、0 HTTP。未到 collector gate，不能宣布候选通过，也不恢复旧 trace 完整性。

独立只读合同审计确认 PID 3189205 的 32 个 kernel、64 次复制、32 次非零 memset、33 个 NVTX 范围完整；512/16384 字节 memset 各 16 次、未匹配数 0，32 条独立输出参考均通过。证据见[已知工作法证](result-analysis.md)，原 JSON 的路径及 SHA 见 [summary.json](summary.json) 的 proofs。这是终止后对已有数据的局部合同检查，在线阶段尚未写出自己的合同收据；没有补做负控。

实际目标仍有 CUDA/NVTX 两条完整性 Warning。此处直接启动 CUDA fixture，没有非 CUDA wrapper，因此不能把这两条 Warning 归为 wrapper 的空事件提示。

NVLOG 记录的顺序为：

- 主线程 3189205 在 ProfilerStop 处使用 flag 0（第 4425–4427 行）。
- 控制线程 3189209 随后使用 flag 1（第 4637 行）；本机已冻结 CUPTI 13.3 头文件把 1 定义为 FLUSH_FORCED。
- 同一控制线程发出 CUDAFinish，记录 ReportCUDADone DONE，再发出 NVTXFinish（第 4682–4684、4742 行）。
- 应用退出时又发生两次 flag 1 和第二次 CUDA Done（第 9717–9726 行）。

这直接支持“普通 flush 后存在控制线程 forced flush”的顺序，也证明 producer 确实发出了 CUDA/NVTX 终态；**尚不能证明 Warning 的内部触发条件，或证明 importer 收到了终态**。完整已知工作与 Warning 共存，不能自动把通用 Warning 判为误报。CUDA Done 也不替代 NVTX 的收尾证据。其余三组未运行，无法判断外部 stop 或移除 wrapper 是否改变结果。

producer 记录 304 CUPTI events、buffers in use 0，importer 记录 292 CUDA events、33 NVTX events；两种计数的类别范围未闭合，不能相减宣称丢了 12 条。未观察到明确的 drop/incomplete/parse/allocation 数值，不补成零。原 NVLOG 还保留 activity kind 33–36 的 context-disable Error，以及 HES/CUPTI_ERROR_NOT_SUPPORTED；SQLite 明确使用了 legacy software trace。这些消息没有被证明是两条完整性 Warning 的原因。

监控 6 次尝试、5 次成功 flush。失败是同身份 PID 3189205 / start_ticks 99613070 的 pinned `/proc/PID/io` open EACCES，初读及即时复查均为 R；49 ms poll 实耗 49.064 ms，没有 pidfd 退出事件。FDSize=0、缺少 Vm 字段、stat vsize=0 强烈指向退出中的 mm/files 清理窗口，但不是错误时已终止的证明，也不支持“权限配置改变”。冻结 50 ms 预算未获得终态，维持 ENV_ABORT 正确。

本审查只运行一次离线解析器并补人工判读，没有重新 export、profile、HTTP、GPU、硬件采样或新负控。所消费的 run 原始文件 seal 无差异。NVLOG global 100 会扰动时序，不能将本 fixture 用于性能分析；正常两 head 重复收益仍仅按此前有效同身份材料复用。四组机制比较及模型 GPU 对照均不完整，不据此扩大采样或修改监控预算。
