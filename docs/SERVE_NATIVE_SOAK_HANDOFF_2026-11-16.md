# 当前默认原生长稳交接

当前rev260包`168477d190e4b0d2c5f2f92410722b39486801500ad4397ff1c717019f022d64`
已完成客户端审核元数据及恢复终态修复，12项新CPU、4项旧回归及
精确封装同组12项通过；发布意图维护另8项CPU通过、真实归档后
原失败步骤续做完成，13项成功命令和1次原容量失败保留，0新增GPU。
引擎/控制/native/profile与rev254逐项相同，复用原5启动20请求的
[真实业务证据](evidence/serve-video-projective-composition-20261117/README.md)。
[rev260交付](evidence/serve-client-receipts-delivery-20261117/README.md)
记录实际回退、再前进和预算保留；没有把CPU夹具称为新模型验收。
连续60秒长稳驱动短验仍属于rev188，[原证据](evidence/serve-native-soak-business-20261116/README.md)按原身份保留。

[当前独立day prepare/check](evidence/serve-client-receipts-day-handoff-20261117/README.md)
已完成，24小时未运行。快照固定`a94d5fc0`及rev260包，不依赖活动
工作区和未来默认指针；原rev254及更早快照保持。

## 当前已准备的入口

私有验证服务UUID `1aaf062e-ad9e-461e-931c-72ea7094498b`，生产默认保持STOPPED。
先进入保留的源码快照；此路径不能随清理开发分支而删除：

```bash
cd /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-client-receipts-20261117/validation-src
/usr/bin/python3 -I -B tools/serve_soak/native_stage.py check \
  --plan .q4t-work/day-01/plan.json
```

准备好连续使用GPU后，在该快照中执行：

```bash
/usr/bin/python3 -I -B tools/serve_soak/native_stage.py run \
  --plan .q4t-work/day-01/plan.json
/usr/bin/python3 -B tools/serve_soak/native_report.py \
  --output .q4t-work/day-01
```

run持有跨工作区同一严格GPU租约，不需要systemd/nginx，不安装后台
恢复任务。固定24小时1440周期/7200业务生成加1自检，每分钟一组，
保持原配额；第一小时60周期/300业务请求单独记录。最多1模型启动，
0探测，总期限87900秒；不重放请求或重复工具，不把short称作长稳。

入口前台运行。中断/监控或证据故障只停止本次自有服务，保留失败。
终端丢失时，在该快照目录用以下入口查询或停止私有状态：

```bash
./build/service/bin/q4t service status --state .q4t-work/day-01/state
./build/service/bin/q4t service stop --state .q4t-work/day-01/state
```

每周期progress只表示已写进度，须结合实时status。完整通过还要求
原始wire、真实工具历史/来源、持续监控、同一模型身份、资源/配额与
STOPPED清理全部复核。RSS/RssAnon相互重叠，不能推导统一DRAM硬上限；
相关R08任务继续。不要在此快照构建或切换默认；build是精确包引用。

## 后续代码或发行变化

此计划继续验证rev260，不会因活动开发工作区改变而失效，也不会
自动跟随新默认。若要验证后续发行，保留此快照、包和原证据，按
[prepare流程](../tools/serve_soak/README.md)创建新源码快照及新目录。
不得改旧plan散列、清空execution或把rev260证据移称新版本证据。
原rev254/rev244/rev234/rev224/rev214/rev204独立快照、工作区day-01及更早计划保留，当前有效入口以上述独立快照为准。
