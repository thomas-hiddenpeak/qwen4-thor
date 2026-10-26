# Nsight collector lifecycle controls

The automatic API-stop/report-ready repair is frozen in
`docs/MTP_SUFFIX_AUTOMATIC_2026-10-26.md`. It keeps manual control available for
historical plans, selects one lifecycle before the stage, and never promotes
shutdown into capture success. All actual-target warnings and memory-coverage
gates remain blocking. CPU46 passed once. The single stage ended after 53.061789 seconds. It
generated/imported the direct report and returned to Launched, but no callback
receipt arrived within 45 seconds. Cleanup then failed; ENV_ABORT, zero models,
HTTP or exports. Registration is proven; callback dispatch remains unknown.
No new capture is authorized by this failed stage. See the stage document.

This is a bounded tool diagnosis, not an inference benchmark or a replacement
for the suffix performance acceptance matrix. The frozen scope and budgets are
in `docs/MTP_SUFFIX_NSYS_2026-10-24.md`; the bounded monitor repair and rerun
are recorded in `docs/MTP_SUFFIX_NSYS_RERUN_2026-10-24.md`. The latest two-control
interactive stage and its failed outcome are recorded in
`docs/MTP_SUFFIX_DELIVERY_2026-10-26.md`.

`prepare_nsys265.py` binds one isolated 2026.5.1.161 dependency candidate and
reuses unchanged CPU proofs and the large parent snapshot. Its two fresh
controls and conditional six HTTP requests retain the existing strict gates;
see `docs/MTP_SUFFIX_NSYS_265_2026-10-26.md`. The new version documents hardware
trace as the default for `cuda` on supported Blackwell GPUs, with possible
software fallback. Static help does not qualify the actual backend or trace.
The one 2026.5.1 run also rejected the first interactive stop and ended after
8.052780 seconds with ENV_ABORT, zero model loads and zero HTTP. Both old and
new target logs explicitly report HES failure followed by software fallback;
this is not a new causal explanation. No failed report was exported or retried.

`fixture.cu` executes 32 independently checked kernel operations, 32 H2D and
32 D2H transfers, 32 nonzero memsets (512/16384 bytes alternating), and 33 named
NVTX ranges. Its ordinary CUDA allocations occur
before capture. After `cudaProfilerStop`, it waits at most 60 seconds for its
owner's release file and then frees its allocations. The receipt independently
binds every output, CUDA status, PID, TID and normal release.

`fixture_audit.py` checks exact counts, IDs, transfers, context/stream ordering,
and both directions of API/activity correlation. Independent SQLite derivatives
remove a complete launch/kernel pair or the final NVTX range. Their rejection
does not simulate or prove the absence of native buffer loss. Diagnostics stay
separate: known fixture work may be complete despite a warning, but that never
exempts an arbitrary model trace from its integrity gate.

The historical `prepare_stage.py` binds the unchanged f0ff observation binary,
previous passed
HTTP/CPU evidence, repaired monitor, new tools, fixture binary and actual Nsight
libraries. `run_stage.py` retains one repository-wide GPU lease through all
four lifecycle controls, conditional model captures and final telemetry flush.
The frozen candidate was the wrapper with explicit named-session stop.
The rerun observed a lifecycle rejection after sending `nsys stop` to a
`nsys profile` session. The initial general incompatibility explanation was too
strong: NVIDIA support also describes named profile/manual-stop combinations.
The exact local configuration remains unaccepted. An explicit interactive
`start`/`launch`/`stop` lifecycle was separately implemented
in `interactive.py` and `prepare_delivery.py`, but the same internal rejection
also occurred with the correctly interactive lifecycle on 2026-10-26. The earlier
mode mismatch is therefore not a sufficient explanation. Unresolved target
diagnostics stop the stage; it does not choose a more favorable variant.

The six conditional HTTP requests are subset 8K OFF, 8K ON, 200K ON and 200K OFF;
each 200K service first handles one uncaptured 8K request. All use the existing
evalscope collector and frozen output identities. No repeat-benefit samples,
runtime optimization candidate, deployment or new five-tier claim are added.

`analyze_gpu.py` is offline only. CPU/NVTX identity keys associate the spans;
CUDA execution and API timing stay on the Nsight clock. Thread CPU can include
submission and busy waiting, and observed GPU envelope gaps are not automatically
CPU submission costs. The analysis cannot convert profile time into normal
throughput or infer that old 200K/8K changes have been repaired.

The first 2026-10-24 run ended with `ENV_ABORT` after 5.229 seconds: monitoring failed
while the first fixture was exiting. Its known work was complete in the saved
trace, but actual-target CUDA/NVTX integrity warnings remained. Only synthetic
CPU negative controls ran; the real capture negatives, other three variants and
all model captures remained unmeasured.

The separately frozen rerun repaired the exit window using a bounded ledger of
same-identity PF_EXITING tasks, retaining missing io until the original pidfd
and pinned terminal state confirm exit. It passed 97 CPU methods and a fixed
64-child native run with 15 resolved pending cases. The GPU stage flushed 11/11
samples but did not exercise the new pending branch. Both ordinary-stop variants
closed the known-work and real-copy negative controls; actual-target warnings
persisted. The third variant failed with `Range collection stop is not expected`
and required owned-target forced cleanup. The stage stopped after 10.026 seconds,
with `ENV_ABORT`, no model loads or HTTP requests, and no retry. Neither the
warning classification nor the subset 8K/200K GPU comparison is complete.

The 2026-10-26 stage passed 32 affected CPU contracts, reusing 86 unchanged
contracts and the separate native64 evidence. `prepare_delivery.py` freezes
only two new interactive controls and conditionally four model loads/six HTTP
requests. `run_stage.py` rejects the old profile-plus-external-stop path before
starting it. The stable owner verifies subreaper status and actual target ancestry;
control exit, target exit, report existence and trace coverage remain separate.

The sole new stage stopped after 7.042835 seconds at its first direct control:
start succeeded, but `RangeCollectionState::Stop` rejected the same named stop.
Forced cleanup triggered report generation and automatic session shutdown; the
later explicit shutdown found no active session. Monitoring flushed 8/8 samples,
including two preserved same-identity terminal io denials, with no pending entries.
The report was retained but not exported. No model or HTTP request ran. Seven
recorded process identities were absent at the independent audit and the lease
was released; absence does not supply the missing target wait status. The stage
remains ENV_ABORT, with no validated collector fix or new performance claim.

The follow-up official-source review found a related 2026.5.1 multi-process
CUDA-stop flushing improvement. One exact ARM64 CLI package is isolated under
`.q4t-work/suffix-nsys-dependency-20261026/`; it has not been executed or qualified.
See `docs/MTP_SUFFIX_COLLECTOR_DEPENDENCY_2026-10-26.md` for package identity,
the corrected compatibility inference and the unchanged integrity requirements.

同步收尾候选使用 `synchronous.py` 和 `prepare_synchronous.py`，固定
`repeat:1:sync`，实际目标 APIStop 返回后收据确认报告生成，再按唯一
Generated 路径绑定输出。旧模式与证据保留，warning/memory 门不变。
范围与结果见 [同步报告合同](../../docs/MTP_SUFFIX_SYNCHRONOUS_2026-10-26.md)。

报告绑定缺陷修复由 `prepare_report_binding.py` 冻结：同步 APIStop 屏障后
绑定唯一 owned 报告，不要求 Generated 及时出现，矛盾仍拒绝；选项和
完整性门保持。见[本阶段记录](../../docs/MTP_SUFFIX_REPORT_BINDING_2026-10-26.md)。
32 CPU合同和两组真实同步绑定通过；已知工作齐全但实际目标warning
仍在，完整性门停止。全审计保留一次FAIL：direct的早期NVLOG整文件
收据后又追加daemon日志；精确前缀核对通过，目标记录未变。不可将
绑定修复、局部核对或producer NVTXFinish当任意trace完整性证明。

`mechanism_probe.cu` / `mechanism_analyze.py` / `mechanism_stage.py` 是固定
M4/N48/K2560、32MiB的参数诊断候选，分别运行CUPTI-only与条件Nsight；
没有吞吐主张，也不能将一个进程的参数填进另一个进程的correlation ID。
`nvlog_snapshot.py` 保存不可变早期前缀，最终daemon日志单独核对。
本轮CPU55通过，但独立探针漏链生产HcInjectGev，构建额度后停止，
0GPU/模型/HTTP，尚无可运行probe或真实新采集资格。后续最小依赖修正
及完整边界见[固定参数诊断](../../docs/MTP_SUFFIX_MEMSET_PARAMETER_2026-10-26.md)。

`nvlog_stream.cpp` / `nvlog_stream.py` 提供固定参数、有限内存的无损FIFO日志流；
`recorded_gpu.terminal_evidence_lines` 逐行解析并保留完整逻辑SHA。44项CPU和
一次真实旧日志往返通过；尚未证明实际NVLOG FIFO兼容性，6E仍拒资格。
`gpu_pair_stage` 清理使用独立60秒预算，原测量失败不能被清理成功擦除。
见[CPU修复结果](../../docs/MTP_SUFFIX_LOGSTREAM_2026-10-26.md)。

流式真实采集由`gpu_pair_stage`显式`nvlog_stream`计划启用；
`stream_recorded_gpu.py`保留strict失败和未知实参，只作明确限定的区间诊断。
实际首8K新增4条收尾错误，受限资格INVALID，后续臂未启动，不能当完整性通过。
Thor驱动/opt依赖继承与ENV_ABORT分类已修复。见
[流式GPU补测结果](../../docs/MTP_SUFFIX_STREAM_PAIRS_2026-10-26.md)。

2026-10-26最终补齐200K OFF，与保存ON恢复结果及已有8K组成受限对照。
`remaining200_off_v1`绑定保存ON分析/终审，只允许1启动/2HTTP；整步1680秒
包含840采集、60清理、120导出、600分析和60预留，阶段1800秒。
38CPU一次通过；实际完整驱动980.140969秒，原始7.085GB输入筛选压缩
173856004字节，EOF/收据/报告正常。历史大日志按内容复用，ON不重采。
严格FAIL、六错误与8732未知memset仍保留；仅已记录活动可以对照，
没有全trace完整性或性能接受。两head收益证据复用，优化保护NO_GO，
保留默认版本并结束本轮有界定位。见[最终审查](../../docs/MTP_SUFFIX_FINAL_OBSERVATION_2026-10-26.md)。
