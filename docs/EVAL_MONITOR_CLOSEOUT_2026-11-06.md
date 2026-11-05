# 验收监控收尾：范围与交接

2026-11-06，按用户“先做收尾再考虑下一项优化”执行。

## 冻结范围

这是测试工具可靠性修复，不改变推理运行时或性能目标。仅处理：

1. 旧suffix阶段驱动在采样线程join后复核失败，避免晚到错误仍标记成功。
2. 将已有包装层的退出后完整采样、join后审核及只读分析门禁整理到
   `tools/evalscope/monitored_stage.py`；原run_rerun入口调用公共实现。
3. CPU故障注入、真实历史失败的只读拒绝检查、文档与独立交付。

本轮模型加载、HTTP、GPU、profile及五档性能请求预算均为0。
本机针对性CPU测试一次成组执行；仅具体失败修复后重验受影响项。
不启动MTP输出行裁剪、QSA融合或量化head补验。原研究分支和证据保留，
工具提交可单独审查，不带入未验收kernel或客户端改动。

## 合同

- 成功收尾前必须有采样开始于服务/子进程结束之后的完整落盘观察；
  等待同时受资源采样最大年龄和原阶段绝对截止时间约束。
- 采样线程join后重新检查失败列表、清理错误及summary；未确认退出
  身份、未落盘样本、缺失/损坏summary均不能产生可接受的完成记录。
- `postflight.json`写在原sealed output外；原state、summary及seal不改写。
  分析前同时核对两份原件摘要、成功状态与最终采样屏障。已有输出或
  postflight拒绝复用，历史decision不会由监控检查改写。
- `MEASURED_REQUIRES_REVIEW`仅表示采集可继续审查，不代表性能、质量
  或外层GPU租约通过。GPU工作仍经原共享锁入口；轮询不能排除短暂干扰。
- 旧驱动直接调用也能拒绝join期间新增错误；完整屏障与summary合同
  要通过公共入口。其他历史专用驱动未批量迁移，不追认旧证据。

## 验证与交付

工具合同已通过，结果为`TOOL_CONTRACTS_PASSED`。本机验证如下：

| 检查 | 结果与范围 |
|---|---|
| 公共入口新回归 | 20通过；包括真实线程驱动、晚到错误、屏障/截止、摘要、pending、损坏/缺失/不可写记录及回调恢复 |
| 原telemetry回归 | 38通过、1明确跳过未选择的64记录重放；包括两个自有CPU进程探针 |
| 原exit_pending回归 | 24首轮通过，1因隔离工作区缺历史夹具失败；复制原a533d3bd摘要副本后仅该项重验通过 |
| 公共host注册 | CMake配置及定向CTest通过，实际执行20个新回归，不重复计入唯一用例数 |
| 旧驱动负控 | 使用d8880693原代码，晚到错误回归按预期失败`EnvironmentAbort not raised`；修复后通过 |
| 真实历史ENV_ABORT | 新只读门禁拒绝量化head失败plan；plan/state/summary/原decision四份摘要未变 |
| 入口与部署身份 | 原run_rerun CLI可导入，运行时代码零差异，默认ELF c1bdb0a7/sidecar 0efc2c0d不变 |

共83个唯一CPU用例通过、1跳过；原夹具失败、预期旧代码负控失败及
完整日志均保留。不是84项全过，也不是模型/GPU验收。
可核验来源与12份记录摘要见[验证清单](evidence/eval-monitor-closeout-20261106/validation.json)。
模型加载/HTTP/GPU均0。量化head此前正式状态仍为
ENV_ABORT_MONITOR_FINALIZATION；该工具修复不会使其自动通过。

独立交付分支`codex/eval-monitor-closeout`只包含工具、host注册与文档。
量化原型仍在`codex/mtp-quantized-head`，不整体合入；旧阶段计划的
来源指纹不得改写，今后执行需要重新冻结包含公共模块的新计划。
下一项性能优化等待后续选择，本次不自动启动。

## 使用

阶段特定校验、请求预算、来源冻结及业务审查仍由调用方负责：

```python
import monitored_stage

monitored_stage.run_stage(legacy, telemetry, plan, plan_path,
                          audit_server=audit_server)
# 独立分析进程也必须在生成decision之前调用：
monitored_stage.require_complete_monitor(plan)
```

默认postflight位置为plan文件的父目录；若它不等于output父目录，
独立分析须显式传`postflight_path`。同一Python进程不并发运行阶段，
临时sampler/auditor回调在任何退出路径均恢复。
