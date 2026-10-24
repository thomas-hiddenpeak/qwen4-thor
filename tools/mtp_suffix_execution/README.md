# 有界执行观测阶段

当前状态（2026-10-24）：首轮在4条完整HTTP响应后因监控EACCES **ENV_ABORT**。
程序零警告构建；CPU/NVTX/Nsight开启路径尚未执行。`analyze_results.py`及
`analyze_trace.py`仅静态/AST审查，未实际运行；后续使用前单独冻结身份。
[阶段结果与证据边界](../../docs/MTP_SUFFIX_EXECUTION_2026-10-24.md)。

本工具仅诊断已有固定 k3/min32 suffix 的 CPU 提交、等待与 GPU 执行。
只构建一次观测程序，`Q4T_EXEC_DIAGNOSTIC` 默认关闭；不改变数学计算、
同步边界、head 选择或默认交付程序。不包含自动优化候选、条件 B 或部署。
全部输入均为已观察的开发材料，不是新的性能验收集。

## 冻结矩阵

总上限 **49 HTTP、13 次服务启动、7200 秒**。无预热、重试、补采、
参数扫描；缓存自然继承，startup auto，不调整 affinity、功耗或缓存。
每次输出 256 token，逐响应核对输入 token 数及旧 6a 同 head 完整输出 SHA。
重复三请求保留全部原值、顺序和首请求，不只检查一个代表 hash。

| 顺序 | head / suffix | 条件 | 固定请求顺序 | 请求数 |
|---|---|---|---|---:|
| 1 | full ON | 全部观测开关关闭；首项真实 evalscope HTTP | 8K | 1 |
| 2–3 | full OFF→ON | 普通运行 | code×3、text×3 | 12 |
| 4–6 | subset OFF A1→ON B→OFF A2 | 普通运行 | 8K、200K、code×3、text×3 | 24 |
| 7–9 | subset OFF A1→ON B→OFF A2 | CPU 双钟观测 | 8K、200K | 6 |
| 10–11 | subset OFF→ON | CPU + NVTX + Nsight | 8K，捕获本请求 | 2 |
| 12–13 | subset ON→OFF | CPU + NVTX + Nsight | 8K 前驱不捕获，200K 捕获 | 4 |

8K/200K 的实际输入为 8170/204781 token。所有服务 B1、greedy、MTP k=3，
完整 head 默认；子集明确传入原 65536 行 BF16 产物。普通与插桩结果分别报告，
不混合为收益样本。旧 MTP/LAYER timing 始终关闭。

普通重复场景描述目标：full 每个 code/text 的 ON/OFF 中位比至少 1.05；
subset 每个 B/A1、B/A2 都大于 1，描述性配对几何均值至少 1.05。
共享 B 不构成两个独立块，同一启动内三请求不构成三次启动；无总体非劣结论，
不从 A/A 极差制定容差。不以重复收益抵消任一其他保护观察。

## 冻结与执行

先由阶段所有者完成必要构建、源码范围审查、Nsight 静态能力记录；
prepare 只冻结文件、复制开发夹具并发现监控源路径，不运行推理或测试。
`--reuse-review` JSON 必须含 `observation_only: true`、
`unchanged_math_and_default_off: true`、原 6a 的 `reference_binary_sha256`，
以及显式逐项审查的 `allowed_changed_paths`。旧 22 HTTP 质量、86 host、10 阶段
合同仅按未变数学依赖的原覆盖范围复用，不代表新程序整模型质量通过。

```bash
python3 -B tools/mtp_suffix_execution/prepare_stage.py \
  --stage .q4t-work/suffix-execution-20261024/diagnostic-v1 \
  --binary .q4t-work/suffix-execution-20261024/build/q4t \
  --binary-source-commit ACTUAL_RUNTIME_SOURCE_COMMIT \
  --build-dir .q4t-work/suffix-execution-20261024/build \
  --build-log .q4t-work/suffix-execution-20261024/build.log \
  --reuse-review .q4t-work/suffix-execution-20261024/reuse-review.json \
  --nsys /usr/local/cuda-13.3/bin/nsys \
  --nsys-capability .q4t-work/suffix-execution-20261024/capabilities/nsys-capability.json \
  --extra-file docs/MTP_SUFFIX_EXECUTION_2026-10-24.md

python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/suffix-execution-20261024/diagnostic-v1/lease -- \
  python3 -B tools/mtp_suffix_execution/run_stage.py \
  --plan .q4t-work/suffix-execution-20261024/diagnostic-v1/frozen-plan.json
```

实际路径以冻结计划为准。额外传入当前判定文档、开发日志和构建身份文件。
离线分析器不属于测量依赖，阶段结束后执行前单独封存其 SHA；不得修改原始证据。
全量 src/include/cmake、测量工具、CMake 构建元数据、
旧参考输入/输出身份、candidate、默认父程序和原子集身份均冻结。
新程序有独立目录，默认 f356、其发布 sidecar 和子集产物不替换。

整个驱动、服务间隙、profile 导出、退出和最终监控屏障持同一共享独占锁。
阶段/单组截止触发后只清理自有进程并保留部分证据，不因 trace 不完整重跑。
7200 秒包含采集、正常退出、导出、最终验证与最多 5 秒的末尾新采样等待；
异常后的必要自有进程清理仍须完成，记录实际超时，不声称阻塞系统调用具有硬实时界限。

## Nsight 与完整性

冻结使用本机 Nsight Systems 2026.1.3：CUDA/NVTX，禁用 CPU sampling 和
context switch sampling，`cudaProfilerApi` 单范围，`capture-range-end=stop`、
`--kill=none --wait=all`。仅 profile 组设置 `Q4T_EXEC_NVTX=1` 和指定
`Q4T_EXEC_PROFILE_INPUT`；CPU 组只设置 `Q4T_EXEC_DIAGNOSTIC=1`。

Nsight 启动 Python target wrapper，再启动实际 q4t，保留注入环境。
wrapper 独立保存实际 q4t PID/start_ticks 和退出码；主驱动只在 pidfd 身份及
executable 核验后向 q4t 发送 SIGTERM，等待包装进程与 Nsight 自然退出，
不提前终止 launcher。仅异常清理会使用自有步骤进程组兜底。
四份 `.nsys-rep` 逐一导出 SQLite，保存命令、退出码、hash 及非空 kernel/API/NVTX
表证据；无自动替代 profiler 或补跑。完整性审计还须核对实际 q4t PID、
捕获目标、丢失记录和请求范围，表非空本身不证明完整捕获。
导出当场检查 DIAGNOSTIC_EVENT、severity 和 profiler 日志；丢失、截断、
不完整、错误或未解释 warning 均停止。只有精确的 Unified Memory trace
unsupported 消息作为未请求的辅助特性缺失单列，不替代 CUDA/NVTX 完整性。

`execution-audit.json` 检查 batch/span/flush 一一对应、4096 条容量、
complete/closed/时钟错误、父子区间、每请求 prefill/init/free 及所有 chunk 覆盖。
capture begin/end 必须各一次、error=0，绑定实际 q4t PID 和指定输入长度。
normal 出现任何 exec 日志或旧 intrusive timing 即失败。异常也保存部分批次、
实际退出与失败原因；诊断缺项不能被当作零耗时或性能失败。
复用 admission 审计继续核对实际请求计数、min32、完整 head/子集合同、
index/table 释放和无静默回退。

最终监控屏障在最后服务退出、导出和第二次文件核验之后，要求新样本 start
及 flush 都晚于该屏障；检查采样线程 join、最终 summary 和无 unflushed attempt。
环境竞争、监控或自有退出清理失败记录 ENV_ABORT，不宣称观测有效。
轮询不能排除短于间隔的干扰。

## 分析边界

- CPU wall−thread CPU 是 non-running wall，含阻塞和失调度，不等于 GPU 时间。
- ModelPrefill、完整 prompt DraftExtend 等 call 可含内部同步；父子 span 不能相加。
- CUDA API wall 与并行 GPU 执行重叠，不相加；GPU busy 用 kernel 区间并集，
  memcpy 单列，不把所有 kernel 时长之和称为 elapsed。
- Nsight profile 及 CPU 双钟开销单独记录；普通 HTTP 才用于本轮重复收益描述。
- 显式报告证据有效性、测量完整性、统计结论、工程决定。缺乏可辨识机制时，
  保留根因未知和默认交付版本，不自动延长预算或创造优化候选。
