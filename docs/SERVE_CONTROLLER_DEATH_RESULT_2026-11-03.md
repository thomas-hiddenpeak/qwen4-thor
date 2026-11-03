# SRV-16 独立控制器死亡限定接受

2026-11-03：冻结驱动4c780bef实际通过。独立前台生产controller被
SIGKILL时，已有活跃输出与已准备的grammar排队请求均收到完整
server_shutdown错误，后端自然排空并回收4个grammar子进程；验收
程序没有向worker/backend/grammar补发清理信号。10秒观察无重启，
GPU和原锁已释放，SOAR恢复服务仍持久masked/inactive。

## 改动与证据身份

本批只新增验收工具与证据，生产托管Python与211个C++来源均未改。
默认ELF d66f2bb9、sidecar42d7672a和protocol55a3e67b保持；数值、模板、
SDK及SRV-15生产CPU51/guard30证据按相同来源复用。

新v2包manifest
`35ca281fb34b5deb48b2acd2dfd82e2e9a4c8de522c9049ec114b0b182061979`，
默认1GiBcritical门限、restart_limit3、loopback18122，full-head MTP k3、
seq1/8192/2048等文本配置保持。包的prepare未验声明不改写，独立
[接受记录](evidence/serve-controller-death-20261103/accepted_package.json)
绑定该manifest与实际结果。本轮没有内存压力，不能替代1GiB边缘退场
证明；旧30GiB保护停止验收保留为独立证据。

新增CPU最终8项有效通过：真实pidfd_getfd/flock生存期、错误身份/
非直接子进程/共享锁拒绝、监控错误和竞争ENV_ABORT、未知清理拒绝、
已死controller的PGID复用以及不完整/错误SSE终态拒绝。首轮括号
导入错误、SSE正夹具created/logprobs错误和隔离入口缺referencing
均保留；仅修复受影响夹具/入口，不追写原通过。7项AST/实现未变
复用，重验1项；exec helper拆为仅stdlib/生产辅助依赖后-I导入通过。
当天日志曾提前写“隔离入口通过”，后续纠正条目及原失败一起保留。

## 实际结果

| 项目 | 验证结果 |
|---|---|
| 模型/请求 | 1启动、6生成尝试；4成功、2预期shutdown错误，无自动重试 |
| 业务 | 普通JSON、严格JSON、真实MODEL资料读取工具和答案回填通过 |
| 故障前真实负载 | 已收到非空普通SSE content，slot占用，queued1、grammar worker1、preparing0；两个客户端仍活跃 |
| 故障注入 | 仅向本轮controller的已核验pidfd发送SIGKILL，实际returncode=-9 |
| 自然退场 | worker STOPPED/cleanup_confirmed，backend shutdown0；两请求各完整server_shutdown+DONE，无成功finish或usage终态，grammar4/4回收 |
| 重启/外部清理 | attempts1/restarts0，即使预算3仍未替换；10秒无复起，helper_cleanup_signals=0 |
| 设备/锁 | NVML最终空、监听关闭、自有进程退出，原inode3294067可重新flock |
| 时间/控制 | 业务及10秒观察窗口61.219秒；含最终监控/锁关闭总61.469秒，均低于220/600秒预算；5显式控制+监督HTTP保守188上限 |
| GPU环境监控 | 原guard47次、独立custody21次设备/进程观察；全部无竞争，独立custody COMPLETED，无ENV_ABORT |

真实响应原件、最后资源状态和场景计数见
[结果](evidence/serve-controller-death-20261103/result.json)。本轮GPU首轮
通过，没有追加启动/请求、辅助模型清理或修改原次数/时间预算。

## 对锁、状态与结论的限定

原生产gpu_exclusive guard在模型加载前持锁并监控。ready后验收
进程复制其同一open-file-description，核对controller/worker/backend
各自实际FD，再启动独立gpu_exclusive.inspect/NVML监控。两监控有
重叠后才杀controller，复制的锁到完整清理和停止观察结束才关闭，
没有释放/重新取得锁的空档。它保障本次完整阶段互斥；**不能仅凭
额外副本持锁反推worker自身锁生存期**，该命题另外有CPU证据及
本轮实际描述符检查。轮询不能排除短于采样间隔或不可见竞争。

被杀controller无法写自己的terminal.json或lease/exit.json，原件
保持缺失，不伪造其COMPLETED。service status返回OWNER_ABSENT、
cleanup_confirmed=false，表示控制器自身没有最终证明；worker局部
STOPPED与本轮独立完整资源验证分别保存。此状态仍需运维只读核验，
不能把OWNER_ABSENT直接当清理成功。

原锁文件最后的held元数据也保持为已死controller的历史记录；实际
flock可取得，不能仅凭JSON中的held认定仍被占用。独立
[custody终态](evidence/serve-controller-death-20261103/custody.json)和
[postflight](evidence/serve-controller-death-20261103/postflight.json)保留
最后锁记录、原inode、GPU与SOAR状态，不删除或替换锁文件。

82份原件无损封存、1份本机ELF单列摘要，9份Git证据文件；
[manifest](evidence/serve-controller-death-20261103/manifest.json)记录
每份身份。`python3 -B tools/serve_service/controller_closeout.py verify`
只读核验已存证据，不启动模型/HTTP。没有新systemd unit、常驻服务
或默认pair变更；生产回退包与先前接受不受影响。

本轮证明本机已验配置下的独立前台控制器死亡自然退场，不扩大为
内核不可中断故障、整个进程树同时SIGKILL、持久部署或所有网络环境。
物理RAM硬保护、新ELF小时/天级长稳、实际访问/通知/多人及控制器缺失
后的运维状态核验仍待推进，完整工业化目标未完成。
