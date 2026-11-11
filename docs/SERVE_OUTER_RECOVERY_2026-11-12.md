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
