# SRV-17 失联后的只读运维核验

SRV-16真实控制器死亡后，原终态缺失、锁文本仍为held；实际进程与
GPU已退出、flock已释放。本批补`service.py inspect --run DIR`，把
历史终态、现场进程、监听、GPU和实际flock分开呈现，不修改旧证据。
属于可靠性与运维业务推进，默认C++、模型、HTTP协议和恢复策略不改。

## 合同与边界

- 新v3包增加inspection源码；controller v2元数据在启动前记录有效
  UID、pid/net/user/mnt namespace及proc根身份。v1/v2包仍可核验。
  缺少相同观察范围的旧记录保留UNKNOWN，不能补造历史命名空间。
- PID须同时匹配boot/start；僵尸仍列为残留。扫描同UID、晚于本代次
  的进程，识别同包ELF和托管helper、已确认owner的实际后代；同包
  候选单独列出，不按死进程的旧PGID认领或终止其他任务。
- 读IPv4/IPv6同端口监听、限时NVML查询、原锁实际flock、恢复标记。
  实际锁可用也不能覆盖未知GPU/进程/监听。BUSY只表示当前资源忙，
  不自动归责旧服务。权限错误、PID/记录/资源变化、超限全部保留未知。
- 两次进程/端口快照包围NVML查询。若锁空闲，临时持有自己的只读
  open-description直到检查完成；不创建/删除/重写锁，不解开他人锁。
  此期间合作启动会看到竞争。观察结束即释放，不预留后续GPU容量。
- `history`保持历史终态，`cleanup_observed`仅为本次完整可见范围的
  空闲观察，不改写`cleanup_confirmed`。它不能证明采样间干扰不存在，
  不能替代启动时完整GPU lease或确认外部系统将来不启动其他任务。
- stdout JSON；完整观察（包括BUSY）exit0，缺证/变化/未知exit2。
  不发送信号、不连HTTP、不改运行目录、不执行清理或自动恢复。

## 冻结验收

成组完成后先做CPU合同：真实flock保留/释放、历史held、PID复用、
孤儿同包进程、不可读和缺失元数据、namespace缺失/变化、IPv6监听、
GPU观测失败、记录/锁inode/资源变化。运行受影响托管和包兼容合同；
未变C++与SRV-15/16生命周期证据按SHA复用，不重复故障矩阵。

实际矩阵固定1启动/3成功生成（普通回答+真实项目资料工具与回填），
活跃状态和正式stop后各一次包内隔离CLI inspect；旧SRV-16目录一次
新CLI只读检查应报告缺旧scope而不改原件。现场无新常驻unit/container。
live期允许监控子进程/记录变化导致明确UNKNOWN，同时必须正确观察
本代controller、GPU后端、监听及BUSY锁；不重采样找绿色。stopped期
必须完整且AVAILABLE，live是否完整及原因单独记录。
全程沿用生产controller内部gpu_exclusive guard持锁与监控，显式stop
排空后释放，外部验收驱动不启动第二个模型。原GPU/锁最终空闲，原
SOAR服务仍masked，10秒无复起。限600秒总计、220秒模型GPU窗口、
32显式控制，监督HTTP保守上限188（非实际计数），不扩样找通过。

失败保留目录/来源/日志；只修复并重验失败影响项。通过后绑定v3包
接受记录，保持默认ELF/sidecar，封存CPU及实际wire/控制/GPU证据。
本项不接受长期稳定、物理RAM硬限额、TLS/鉴权/通知或多人部署。
