# 当前默认原生长稳交接

rev188包`509c3bcb9a2c2d7428cbcb9117b109ae58a4087ad304abb0edda73ec74532a74`已完成连续60秒/3组真实短验，
原生outer/Guardian和JSON/SSE/schema/实际文档工具/SHA续答、指标排空、
原始wire复核及清理通过。[完整证据](evidence/serve-native-soak-business-20261116/README.md)。
24小时未运行，由用户选择维护窗口显式开展。

## 当前已准备的入口

私有验证服务UUID `436acc6b-d685-40c3-a47b-322a12240285`，生产默认状态保持STOPPED。
执行前在仓库根目录核对冻结源/默认/包身份：

```bash
python3 -I -B tools/serve_soak/native_stage.py check \
  --plan .q4t-work/serve-ops-v5-20261116/day-01/plan.json
```

准备好连续使用GPU后，由你执行：

```bash
python3 -I -B tools/serve_soak/native_stage.py run \
  --plan .q4t-work/serve-ops-v5-20261116/day-01/plan.json
python3 -B tools/serve_soak/native_report.py \
  --output .q4t-work/serve-ops-v5-20261116/day-01
```

run自己持有严格GPU租约，整个验证不需要systemd或nginx，不安装后台
恢复任务。固定24小时1440周期/7200业务生成加1自检，每分钟一组，
保持当前配额；第一小时60周期/300业务请求单独记录。最多1模型启动，
0探测，总期限87900秒；不重放请求或重复工具，不把short称作长稳。

入口前台运行。中断/监控或证据故障只停止本次自有服务，保留失败；
终端丢失时，day-01/handoff.json中的status/stop命令绑定原包和私有状态：

```bash
.q4t-work/serve-ops-v5-20261116/candidate-01/bundle/bin/q4t service status \
  --state .q4t-work/serve-ops-v5-20261116/day-01/state
.q4t-work/serve-ops-v5-20261116/candidate-01/bundle/bin/q4t service stop \
  --state .q4t-work/serve-ops-v5-20261116/day-01/state
```

每周期progress只表示已写进度，须结合实时status。完整通过还要求
原始wire、真实工具历史/来源、持续监控、同一模型身份、资源/配额与
STOPPED清理全部复核。进程RSS/RssAnon相互重叠，不能推导统一DRAM
硬上限；相关R08任务继续。

## 后续代码或发行变化

旧plan按原源和默认身份冻结。若check因后续开发改变身份而拒绝，
保留此计划/证据，通过通用[prepare流程](../tools/serve_soak/README.md)
为当时实际版本准备新目录；不要改旧plan散列、清空execution或冒称旧
证据适用于新包。本次既有short证据仅绑定上方精确发行身份。
