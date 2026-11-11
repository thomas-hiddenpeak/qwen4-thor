# R09：外层启动、整树重建与宿主恢复

R09 属于 Serve 总计划必做项。开发期间不修改已冻结的 R07 候选
与已有运行入口；R07 维护等待时先完成独立合同与针对性 CPU 验证。

## 源码确认的缺口

- `service.launch_owned` 总会向 Guardian 传 `--activate`，自动宿主
  启动直接调用它会覆盖持久 STOPPED。Guardian 已有 `--resume`，
  但尚无对应完整部署入口。
- Guardian 发现遗留 active slot 时冻结为待 reconciliation，当前
  没有整树/跨 boot 核验后退休这些 slot 的生产路径。
- 内置 Guardian 已能恢复模型、控制器和入口，但 Guardian 自己
  或整树死亡后的自动入口、持久外层预算及显式安装/卸载仍缺失。
- GPU lease 不是整树证明。原生 ingress 不持 GPU lease；即使
  锁已重新获得，仍须核验旧 CPU 入口/父死清理或实际自有资源域。
  不能用 `/proc` 一次扫描或 NVML 一次成功替代完整所有权。

## 持续实施顺序

1. R09.1：外层恢复状态/独立 journal。显式启用；每 15 分钟最多
   3 次 Guardian 启动（初次计费），间隔至少 30 秒。超限留下持久
   保护，禁用/启用、重启宿主或改包不能清空；明确停止且清理后可
   人工确认保护，计数仍保留。故障预算不替代原模型/角色预算。
2. R09.2：进程和存量账本核验。明确区分本次连续监护、同 boot
   新外层及跨 boot。旧 PID 身份、未登记授权、进程树/入口资源与
   租约必须核实；只退休已确认结束的原 ticket，保留代次、故障、
   Key、用量与 profile/version 历史。停止优先，未知不能当清理成功。
3. R09.3：统一 `resume`/显式外层运行入口。自动路径只恢复原
   RUNNING，不调用 activate；离线 stop 同样持久生效。辅助观察
   失败不能重启健康核心，外层 I/O 不进入健康模型控制循环；
   不互相拉起监护者，不隐藏后台脚本。
4. R09.4：可选 systemd unit 与无 systemd 的宿主启动器/容器入口。
   生成可审阅配置，安装/启用/停止/卸载显式执行；未选配不强制
   依赖。宿主已经停机时由所选宿主启动设施重建入口。
5. R09.5：实际 CPU 子树/中断/停止优先/卸载无复活验证；围绕变更
   做必要业务短验（不重复模型性能/全量质量），可回退封包交接，
   随后继续 R08/R10 和全部 A/Q/G00 未完成任务。

## 当前实现边界

`outer_policy.py` 与 `outer_store.py` 已开发，冻结 8 项策略 + 4 项
真实私有 journal CPU。策略要求输入原核心账本，拒绝 STOPPED、
quarantine、未退休 slot 与未知清理；它本身不读取硬件、发信号
或启动进程。运行适配器必须在实际锁下提供证据，JSON 不是设备
或模型授权。尚未接入发行，不宣称外层恢复完成。

长时间运行由用户验收；真实宿主重启/外部部署只在现场选配时
进行，但所需代码、短验与交接仍须完成，不能以未部署代替实现。

## 连续进程树保管（开发中）

`outer_tree.py` 使用现有 Linux 系统能力：在专属、无既有子进程的
单线程外层设置 subreaper，派生一次明确命令；后代即使 setsid/
双 fork，父进程死亡后仍由最近存活 subreaper 接管。
[Linux subreaper 合同](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html)
及 [wait 合同](https://man7.org/linux/man-pages/man2/wait.2.html)为依据，
只实现本项目逻辑，不复制外部代码或引入外部守护程序。

该路径以实际 waitid/waitpid 回收与 ECHILD 为终点；信号只通过
pidfd 发给仍属于当前外层的直接子进程，超时保留 custody。
身份、原启动 attempt、原外层连续存活必须对应；新外层不能通过
复制 JSON 或看见“自己没有子进程”来退休上一外层的 slot。
冻结 5 项真实 CPU 进程用例。完整同 boot 外层丢失/跨 boot 证明、
原 journal slot 退休与运行接线仍须继续实现。

## 实例锁跨根保管（开发中）

仅在同一连续外层内证明旧树为空，还需要阻止其他启动器在其后
插入另一棵树。配置 v5 接受真实继承的 `guardian.lock` FD；
父外层持有同一 open-file-description，Guardian 校验实际 inode、
权限及flock并保留CLOEXEC副本，旧根退出不释放父层实例独占。
这使后续核心 slot 退休能在同一连续实例锁下进行，不能仅凭
JSON 所称的“外层持锁”。后台 detach 不适用该父层生命周期。
当前只开发并验证此原语，未改默认发行、未接自动重启循环。

隔离工作区：`.q4t-work/serve-outer-recovery-20261112/worktree`，
分支 `codex/serve-outer-recovery`，基点1750b278；隔离原因是保留
原工作区 R07 当前plan-v2的源码与真实短验资格。后续合回有效
主工作分支，不把这个隔离入口永久保留。

## 连续外层的原 slot 退休（CPU 已验证，未接运行入口）

启动请求在派生前持久绑定原外层 PID/start/boot、实例锁保管 token、
唯一树 scope、原核心 revision/generations/hash。派生只能消费自己的
原绑定；新树或重新取得的实例锁不能使用旧回执。原外层实际回收
整树并得到 ECHILD 后，`outer_reconcile` 在部署与原实例独占下，
先核对当前描述符、持久外层 intent 和核心格式围栏，再按原 ticket
依次退休 model/controller/gateway。STOPPED、quarantine、代次、
故障记录和冷却不变；预算只允许原核心时钟规则正常过期。

核心 pending rename 或外层最终回执丢失不重置预算，也不发新进程；
仍由同一连续外层/实例证明续办。未记录 spawned 的窗口由派生前
持久 custody 和原活内存 Tree 对应，不以 `owner=null` 认定没有进程。
该未发行外层格式本次补足 baseline/custody，不改已发布核心格式。

冻结28项针对性CPU：8新退休/中断/否定用例、12直接策略/存储回归、
6实例锁与2树原行为；不占GPU。它只解决连续外层路径，外层自身
丢失/跨boot、统一运行和选配宿主入口仍属必做任务。

28项[针对性CPU首过](evidence/serve-outer-reconcile-cpu-20261112/README.md)，4.186秒/0GPU。后续新外层与跨boot仍继续，不把此组完成当总任务终点。

## 外层意外退出与健康业务隔离（CPU 已验证，未发行）

原前台入口使用父死SIGTERM，并写STOPPED；此合同仍适用于普通
前台启动。显式v5外层监护模式另用父死SIGUSR1观察：核对实际
getppid变化后报告outer_owner_lost/READY_DEGRADED，存活Guardian
与核心服务继续，不能把辅助外层退出解释成用户停服。伪造信号
而原父仍活不改变状态；明确stop照常持久STOPPED并清理。

新外层在旧Guardian仍健康时须接管观察，不主动重启它；旧根退出
后仍须实际资源域/跨boot或原连续树证明，不能仅以孤儿标记退休
旧slot。接管与完整运行入口尚未完成，继续实现。

8项[针对性CPU首过](evidence/serve-outer-parent-cpu-20261112/README.md)，5.387秒；必要构建零警告。新观察者/运行接管继续。

## 统一恢复和离线停服（CPU 已验证，未发行）

`q4t service resume` 核对已选封包、部署、实例与原核心账本，只
以Guardian `--resume`恢复RUNNING；人工STOPPED不创建run或取
GPU lease。preflight与实际exec之间的人工stop仍由Guardian
重新读取持久意图处理，不使用activate覆盖。已有故障保护、旧
slot或未退休外层intent仍进入对应核验路径，不能绕过保护。

`stop` 在控制入口已消失、且真实实例/原核心writer可独占时持久
写STOPPED。旧slot、预算、故障保留；stop_durable与cleanup_confirmed
分别报告。写入中断按原journal pending续办，重复stop不增revision。

封包v18含全部当前外层源码；v17来源清单冻结保留，历史包继续
核验。冻结21针对性CPU（8新/13直接，含实际Guardian与隔离封包
导入），不做完整性能或模型矩阵。新外层运行/接管继续。

22项不同[CPU用例接受](evidence/serve-outer-resume-cpu-20261112/README.md)。复核新增未退休外层attempt状态检查，只有直接3项补验；原v17真实候选只读核验通过。自动外层循环与健康根接管继续。

## 跨 boot 旧记录核验（CPU 已验证，待运行集成）

当前boot从实际系统读取，与原外层派生记录比较。只对已确认属于
旧boot的owner/原ticket封存清理检查点，之后按原核心事务退休。
检查点先于任何退休持久化，因此核心rename中断后，即使离线stop
推进了账本时钟，新外层仍能按原ticket续办，不能重新派生或返还
额度。新代次或新ticket会使旧检查点失效；没有GPU就绪授权。

未记录spawned且在检查点前核心时钟已推进到当前boot时，不能仅
靠旧外层记录断言该slot来自旧boot。这一分支仍需实际资源域
证明，属于R09.2/R08待实现内容，不能作为拒绝即完成的出口。

冻结21 CPU（7新/14直接），使用明确标注的历史boot账本夹具，
不重启宿主、不声称实际开机集成验收。运行期仍需按当前内核域
边界接线；[内核cgroup v2合同](https://docs.kernel.org/admin-guide/cgroup-v2.html)
用于后续直接委派适配，不再强制依赖systemd创建scope。

21项[CPU首过](evidence/serve-outer-reboot-cpu-20261112/README.md)，1.283秒；真实宿主重启未执行，自动运行和实际资源域待继续。
