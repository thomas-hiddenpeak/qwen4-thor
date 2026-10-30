# 四项遗留修复验收与交付

范围及冻结预算见[修复计划](../../../docs/MAIN_REAUDIT_FIX_PLAN_2026-10-31.md)。
`repair.py`复用既有evalscope Runner、实际4请求槽健康及封条/原子替换
帮助函数，保留四模式父候选矩阵；不能用已冻结目录追加新的采样。

CPU/build检查通过后执行`repair.py prepare`。`run`和`deploy`必须分别
持完整gpu_exclusive锁，输出为本轮目录下measurement/lease及
deployment/lease；使用绝对路径及/usr/bin/python3。其余顺序为
run→audit→package→deploy→finalize，前一步失败不能跳过。
默认替换前检查旧身份，安装异常自动按已知身份恢复，未知状态拒绝
覆盖。最终封存保留真实构建缓存、运行时源码、工具和旧程序/sidecar。

`rollback.py --package PACKAGE --check`只读核对封条和默认身份。
实际回退需另加新的`--output DIR`并在DIR/lease完整持锁；不依赖
冻结时限，不重跑历史测量。`test_rollback.py`用CPU合成包验证拒绝
未知默认、损坏包、外来恢复标记，以及部分替换恢复和lease要求。
