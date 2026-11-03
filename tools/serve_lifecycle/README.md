# 发布身份与生命周期验收

`catalog.py`只读追溯binary、sidecar与build cache，不执行模型或回退：

```bash
python3 -B tools/serve_lifecycle/catalog.py \
  --output .q4t-work/release-history.json
```

`VERIFIED_TO_ROOT`与`VERIFIED_TO_LEGACY_BOUNDARY`都只说明已列pair身份匹配。
后者的terminal明确`installable:false`：过期历史sidecar不能直接重装。
清单不是原子文件系统快照，也不重新证明每个版本的业务质量。

`deployment.py freeze --bundle <新目录>`冻结当前与直接上一版、构建cache、
v3/v4规则。输出必须在本项目build或.q4t-work的已存在父目录内，已有
产物拒绝覆盖；`inspect --bundle <目录>`只读检查冻结seal及安装状态。
规则只作为版本匹配的已核验文件返回，不自动安装监控服务。

`switch --target current|previous`与`recover`必须在完整GPU lease中运行，
同时取得发布文件锁，检查GPU空闲。示例中的所有路径均换成绝对路径：

```bash
python3 -B tools/evalscope/gpu_exclusive.py --output /ABS/operation/lease -- \
  python3 -B tools/serve_lifecycle/deployment.py recover \
  --bundle /ABS/package/releases --lease-root /ABS/operation \
  --output /ABS/operation/recover
```

恢复只回到bundle的`current`，不重启服务或重放请求。`recover`要求
匹配的durable marker；完整旧pair且无marker时，用显式`switch --target
current`。两文件各自原子替换，不保证跨文件掉电原子性。恢复先检查
全部文件；未知内容、其他部署标记、失去GPU锁或设备忙均拒绝覆盖。
成功切到完整previous后若后续验证失败且不能安全恢复，保留执行记录
与完整previous；不得把它报告为已恢复current。

本阶段固定驱动`stage.py cpu|prepare|launch|finalize`及预算见
[SRV-13合同](../../docs/SERVE_LIFECYCLE_2026-11-03.md)。prepare要求干净的
冻结提交；launch在同一把GPU锁中涵盖全部实例、业务与发布切换。
无自动重试或预算增长；首次lease计算总时限，保留90秒清理余量。
默认生产程序、模型算法与数值实现不变，CPU检查不启动HTTP或模型。
