# SRV-13 停机分类修复与定向补验

原合同见[SERVE_LIFECYCLE_2026-11-03.md](SERVE_LIFECYCLE_2026-11-03.md)。
原05d89984驱动实际完成同PID一小时240周期、1200次成功生成、240次
预检、240次真实MODEL资料查询，固定资源包络通过。之后SIGTERM的
活动请求收到server_shutdown，已准备并排队的strict SSE却收到
service_unavailable。两条流都有错误和DONE；进程退出0、worker回收、
scope清理通过。整体FAILED/guard COMMAND_FAILED保留，不改判通过。
原执行目录已封存，默认3cd3994d/bb620240保持，GPU已释放。

## 修复范围

唯一运行时变化在chat_admission.cpp：seq_stopping分支给RequestControl
设置kShutdown，并返回已有取消路径的-2。它复用HTTP/SSE错误终态、
250ms写截止和count_abort，保持已经先发生的取消原因。没有改模型、
词表、模板、grammar、MTP或正常slot选择。原生fixture验证真实
AllocSeqId的排队停机、已有取消原因、空槽不被占用和队列回收。

修复验收不把原ELF的一小时记录改称新ELF的一小时证明。仅明确复用
未变正常业务路径的小时段、数值/模板和资源包络记录；重验失败路径，
继续原来尚未执行的重启、崩溃和发布切换。新的candidate诊断身份明确
标PENDING，真实业务通过后才生成发布sidecar并安装。

## 固定补验顺序与累计预算

仍使用原6000秒（第一次lease取得时起算）、1224生成、8负响应、
7模型启动、240预检和5000控制观察硬限。不扩预算、不自动重试、
不重放中断请求；原执行1202生成/0负例/1启动及1719观察计入累计。
修复只在剩余时间足够时冻结，deadline前预留90秒清理。

1. 成组构建/CPU合同；原生HTTP和准入24项在补验完整GPU lease内执行。
2. 候选C活动SSE与已准备strict排队请求同时SIGTERM：两条完整错误流
   均为server_shutdown，日志逐请求只计一次取消，worker自行回收。
3. 重启C，旧request_id取消404，普通回答和真实资料工具回填；之后
   对活动和排队请求SIGKILL，journal只读判REQUEST_OUTCOME_UNKNOWN，
   无成功终态、无自动重试、worker退出证明在scope清理之前获取。
4. 再启动C，旧ID404，普通和真实工具闭环，正常停止。
5. 默认A实际切换上一版B（84d16983/c5203331，v3规则），验证普通和
   工具闭环、跨实例诊断不比较counter，正常停止后恢复A。
6. 候选业务全部通过后，生成C发布pair、保留A完整回退pair并安装C；
   实际安装程序再验证普通和真实资料工具闭环，然后停止清理。

补验计划16生成/4负响应/5启动，加原失败运行累计1218/4/6；无新预检。
CPU和实际原生fixture单列。中途失败停止，只有已知文件身份才能回退；
陌生状态或竞争记ENV_ABORT并保留现场，不覆盖其他任务。每阶段有独立
完整lease，原失败阶段已经结束，补验同一驱动内不释放锁。

工具入口：tools/serve_lifecycle/repair.py。原stage.py和已封存原包继续
保留其合同。发布使用现有durable恢复标记与逐文件已知身份替换，不
宣称双文件跨掉电原子事务。仍不证明天级稳定性、物理RAM硬保护、
生产supervisor或公网/多人策略，也不作性能优化接受声明。
