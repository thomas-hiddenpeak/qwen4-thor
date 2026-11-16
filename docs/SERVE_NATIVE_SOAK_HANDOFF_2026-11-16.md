# 当前默认原生长稳交接

当前rev204包`4981551e1e9dc104b9a9833f6b6cd8bd5a059c857cefd6b635e8d75f4a60ecd3`
已完成真实审核主轻、批准SSE/输入block/多choice评分、存储/隔离的
4启动/17请求功能短验及可回退发布；[业务证据](evidence/serve-chat-moderation-business-20261117/README.md)、
[交付证据](evidence/serve-chat-moderation-delivery-20261117/README.md)。原失败另保留，
累计8启动/32请求；不能把原失败链改称通过。连续60秒/3组长稳驱动
短验属于前驱rev188的509c3bcb包，其原始
[证据](evidence/serve-native-soak-business-20261116/README.md)保留，不能
冒称当前审核引擎重跑该连续short。当前原生控制/helper按精确身份复用。

[独立快照day prepare/check](evidence/serve-frozen-day-handoff-20261117/README.md)
已完成，24小时未运行，由用户选择维护窗口显式开展。快照固定
`97850fd8`及rev204包，不再依赖活动工作区和未来默认指针。

## 当前已准备的入口

私有验证服务UUID `93294db6-abf0-47ea-9577-ed128d30c934`，生产默认保持STOPPED。
先进入保留的源码快照；此路径不能随清理开发分支而删除：

```bash
cd /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-chat-moderation-repair-20261117/validation-src
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

此计划继续验证rev204，不会因活动开发工作区改变而失效，也不会
自动跟随新默认。若要验证后续发行，保留此快照、包和原证据，按
[prepare流程](../tools/serve_soak/README.md)创建新源码快照及新目录。
不得改旧plan散列、清空execution或把rev204证据移称新版本证据。
原工作区day-01及更早计划保留，当前有效入口以上述独立快照为准。
