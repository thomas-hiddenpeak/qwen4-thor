# 监控修复后的独立执行观测重跑

本轮已结束：37正常+6CPU请求支持独立局部分析，首profile完整性warning
触发停止（实际44响应/10启动），整体GPU矩阵不完整。详见
[阶段结果](../../docs/MTP_SUFFIX_EXECUTION_RERUN_2026-10-24.md)。
以下为已执行的冻结说明，不授权自动续跑或补采。

本轮是新授权的独立阶段：沿用 [原矩阵与测量口径](README.md)，固定
49 HTTP / 13 启动 / 7200 秒，0 预热、0 重试、0 额外构建、0 自动优化候选。
观测程序仍为 f0ff0fe8，运行时源码 55363f18；旧 6a 参考、默认 f356、
发布 sidecar 及 BF16 子集均保持。原 `spec.py` 和 `run_stage.py` 不修改。

上轮 `suffix-execution-20261024/diagnostic-v1` 保留 ENV_ABORT。它的
4 个原始成功响应、2 次启动和不完整监控不并入本轮数据，不作为省略
本轮首门或其他请求的理由；旧有效合同仍只按原数学/状态覆盖范围复用。

本轮只允许监控、其受影响 CPU 测试、文档和新增准备工具有受控变化。
准备器核对旧计划、原始封存、源快照和审计；检查所有原依赖差分，拒绝
运行时、既有驱动、case/hash/顺序、Nsight 或构建身份变化。
修复监控的 CPU 证明独立保存；旧 11 项测试记录移为 `prior_monitor_fix`，
不能用旧测试结果代替新监控通过，更不能代替真实阶段末尾退出验证。

新 `--monitor-proof` 必须绑定 monitor/test/log SHA，至少 11 项通过且
`tests_failed=tests_errors=tests_skipped=0`、`new_http_or_gpu=0`、
`real_owned_children_reaped=true`；退出确认界限固定每任务 50ms、每采样 100ms。
这里50ms从新分支原IO错误计至最终身份复查，100ms是单采样累计新增poll等待；
不是整体采样耗时上限，旧立即Z/X路径保持。CPU reproduction 的路径、SHA、
状态及是否复现旧失败/观察到非终态权限错误
须显式记录；没有观察到的分支不能写成已经真实触发。

```bash
python3 -B tools/mtp_suffix_execution/prepare_rerun.py \
  --stage .q4t-work/suffix-execution-rerun-20261024/diagnostic-v1 \
  --monitor-proof .q4t-work/suffix-execution-rerun-20261024/monitor-cpu-proof.json \
  --monitor-test-log .q4t-work/suffix-execution-rerun-20261024/monitor-cpu-tests.log \
  --extra-file docs/MTP_SUFFIX_EXECUTION_RERUN_2026-10-24.md

python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/suffix-execution-rerun-20261024/diagnostic-v1/lease -- \
  python3 -B tools/mtp_suffix_execution/run_stage.py \
  --plan .q4t-work/suffix-execution-rerun-20261024/diagnostic-v1/frozen-plan.json
```

第一项推理/GPU 测试仍是无观测插桩的真实 evalscope HTTP。
监控 Bug 的定向 CPU 检查是独立的修复前置，不能被描述成整模型质量测试。
所有启动、间隙、Nsight 导出和最终新采样屏障仍在一把完整阶段锁内。
任一身份/HTTP/观测/退出/监控失败停止并保留现场，不自动修复、续跑或补采。

离线分析器不是运行时测量依赖，阶段结束后运行前单独封存 SHA。
同一组的三请求及共享 B 的两比值保留原统计限制；不得用 ENV_ABORT 的
旧样本补足新矩阵。最终分开记录证据有效性、测量完整性、统计结论与工程决定。
