# SRV-14 实际受控托管限定通过

当前默认`d66f2bb9`/sidecar`42d7672a`、protocol`55a3e67b`保持。
原首轮工具请求503已由[结构化交接修复](SERVE_STRUCTURED_HANDOFF_RESULT_2026-11-03.md)
处理。补验冻结`b6e4ce93`，使用从已接受默认生成的独立包：
manifest `71da1a1d05e3d83dbf28df052c249646e5e80a48639af54b9513debff266af0d`。

实际生产模板的临时user systemd单元启动包内controller/worker/backend，
本次全程一把GPU lease。两个ready点的同controller身份、锁dev/inode、
三方继承FD与cgroup逐项通过；旧后端组退出后才恢复新后端。

| 验收项 | 实际结果 |
|---|---|
| 首实例严格JSON→真实资料工具/答案 | 3成功；资料读取实际执行1次，owner归零、grammar全回收 |
| 空闲后端SIGKILL后的受控恢复 | 仅1次实际替换；同控制器与同一锁，旧组退出 |
| 恢复后的普通JSON及真实工具/答案 | 3成功；新实例独立对账3，资料读取再执行1次 |
| 第二次退出的退避期间正式stop | 取消待启动任务，实际总启动2次，没有第三次 |
| 幂等stop与停止观察 | 原terminal不变；两个退避长度共10秒未复起，MainPID0/NRestarts0 |
| 清理 | guard COMPLETED、terminal STOPPED/cleanup_confirmed；临时unit删除、GPU空、原锁可取得 |

补验实际**2启动、6成功生成、3终止场景、17显式控制**，无HTTP重试或
未知工具执行重放。GPU业务窗口106.920秒，总117.590秒；监督GET保守
上限188、合并显式控制上限205（不是实际观测了188次）。额外只读
SOAR状态查询1次单列。预算保持原220秒窗口、240控制和1200秒总限。
restart预占2 = 实际替换1 + 停止取消的等待1，不是恢复了两个后端。

原3个真实拒绝（抢锁、同目录、错误manifest）及托管29/共享guard30/
驱动8的未变实现证据直接复用。原首轮4次生成的FAILED与清理中断
ENV_ABORT不改写；托管累计4+6=10次尝试，明确超过原8次。修复独立
9次另列，不能混算成原计划首轮通过。

已验证包及伴随接受记录位于：

- `.q4t-work/serve-managed-service-20261103/supplement/package`
- `.q4t-work/serve-managed-service-20261103/supplement/accepted-package.json`
- [原件清单与逐文件身份](evidence/serve-managed-service-20261103/manifest.json)
- [补验结果](evidence/serve-managed-service-20261103/result.json)
- [最终环境检查](evidence/serve-managed-service-20261103/postflight.json)

manifest与源码包不因验收改写；`managed_service_accepted:false`是prepare
时不提供验收声明的标志，后续结论由绑定同manifest SHA的伴随记录给出。
包使用显式restart_limit=2验证有界恢复；一般prepare默认仍为0。
没有安装常驻或开机服务，也没有启用SOAR恢复入口。其持久mask仍为
`/dev/null`、inactive/MainPID0；原锁inode3294067保持，无发布恢复标记。

范围是本机loopback、full-head MTP k3、seq1/8192、inflight8/queued2。
CPU已验证父死通知、锁继承和日志轮转；本轮不补称独立前台controller
被SIGKILL后的真实GPU场景已验。常驻生产访问/通知/多人、新ELF小时/
天级长稳、通用任务恢复、物理RAM硬保护与完整工业化目标仍未完成。
