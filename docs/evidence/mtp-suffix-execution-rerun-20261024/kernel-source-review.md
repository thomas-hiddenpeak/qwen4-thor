# `/proc/PID/io` 退出窗口与 pidfd 修复合同（静态审查）

本审查不修改监控、不运行测试、复现、HTTP、GPU或硬件采样。旧阶段继续为 **ENV_ABORT**。这里证明的是上游内核存在相应窗口，不能回填历史 PID3164108 的缺失现场。

本机 `uname` 为 `6.8.12-1021-tegra`、aarch64、`#1 SMP PREEMPT Thu Aug 6 21:56:04 PDT 2026`。本机只有该版本头文件；以下核对上游 stable **v6.8.12**，未证明与 Tegra 补丁后的实现逐字相同。JSON保留本机header、监控、原法证文件及上游源码SHA。下列行号是下载源码的**物理1基行号**，不是网页抽取器的压缩行号。

## 源码结论

`fs/proc/base.c` 的 `task_dump_owner` 在任务没有 `mm` 时选择root owner；`io` 条目只有owner读权限。因此非特权监控可能在open阶段被拒绝。[task_dump_owner，1814–1871行](https://github.com/gregkh/linux/blob/v6.8.12/fs/proc/base.c#L1814-L1871)、[io条目，3334行](https://github.com/gregkh/linux/blob/v6.8.12/fs/proc/base.c#L3334)。

`exit_mm` 清空 `current->mm`；`do_exit` 先调用它，之后才通过 `exit_notify` 设置 `EXIT_ZOMBIE`。这确立了 **mm已清空但尚未Z** 的源码执行窗口。[exit_mm，538–572行](https://github.com/gregkh/linux/blob/v6.8.12/kernel/exit.c#L538-L572)、[do_exit，858/891行](https://github.com/gregkh/linux/blob/v6.8.12/kernel/exit.c#L858-L891)、[exit_notify，741行](https://github.com/gregkh/linux/blob/v6.8.12/kernel/exit.c#L741)。

这不是“mm为空必定使ptrace检查失败”：`__ptrace_may_access` 的dumpability条件受非空mm保护；io读取函数还有单独访问检查。旧失败记录明确为 **open**，owner/mode路径是合理解释，但不能声称已取得实际内核调用栈。[ptrace检查，340–347行](https://github.com/gregkh/linux/blob/v6.8.12/kernel/ptrace.c#L340-L347)、[io读取检查，2972–2990行](https://github.com/gregkh/linux/blob/v6.8.12/fs/proc/base.c#L2972-L2990)。

v6.8.12的 `pidfd_poll` 仅在整个线程组退出后返回 `POLLIN|POLLRDNORM`，没有HUP分支。当前官方手册另规定reaped后HUP；因此**经过身份绑定的pidfd，其IN或HUP均可代表结束，但不得要求HUP或同时具备两位**。ERR/NVAL不能当退出。[pidfd_poll，2076–2092行](https://github.com/gregkh/linux/blob/v6.8.12/kernel/fork.c#L2076-L2092)、[pidfd_open官方手册，NOTES](https://www.man7.org/linux/man-pages/man2/pidfd_open.2.html)。

## 推荐固定合同

1. 对已识别自有PID先 `pidfd_open`，再打开proc目录，再从该目录读首stat核对expected start；随后 `poll(0)` 记录已完成的活体绑定。这样避免拿已回收旧实例的ready pidfd证明新目录对象结束。`start_ticks` 有滴答精度，不能单独替代内核对象引用。绑定时已经ready的对象继续采用同一pinned Z/X旧路径；没有完成活体绑定时，不启用新的live→exit容错。[pidfd fdinfo/对象与poll实现](https://github.com/gregkh/linux/blob/v6.8.12/kernel/fork.c#L2042-L2092)。
2. 仅process io/EACCES保留完整错误材料后进入新分支；same-identity Z/X保持旧分类。非终态只用已绑定pidfd，在每任务50ms、单样本累计100ms内等待。使用单调固定截止时间，EINTR不能重新开始预算；记录实际等待和调度超时。不要读取pidfd、不要用waitpid/waitid抢走runner负责回收的子进程。
3. immediate pinned stat若已ENOENT/ESRCH，可保留错误并使用**事先完成绑定**的pidfd取得退出证明，不能把消失本身当证明；若冻结实现选择不覆盖，应明确这仍可能导致保守中止。EACCES/EIO/解析错误和身份变化仍失败。确认退出后的最终stat也只通过原目录读取，允许带证明的reaped缺测，不重新打开数字PID路径。[pinned proc描述符语义](https://docs.kernel.org/filesystems/proc.html)。
4. 成功只标 `TERMINATED_AFTER_IO_DENIAL`，保留原EACCES/原始状态/时间/flags/缺失io；不重读io、不填零，也不声称错误时已Z/X或已证明权限原因。超时、ERR/NVAL、未绑定或错误fd、非io错误、EPERM/EIO及稳定源失败均继续ENV_ABORT。所有失败同样保存部分row与复核原文；全部fd在finally关闭。

## 必须保留的反例与边界

- 活进程凭据或dumpability变化后持续存活：必须失败。它若在50ms内随后结束，只证明终止缺测，不证明EACCES由退出造成。
- PID在两次open之间复用：start不符即停；不能使用ready旧pidfd覆盖新对象。首stat与poll绑定材料要进入证据。
- leader退出但其他线程仍活着：process pidfd未ready是有效情况，不能把leader的Z解释成全组结束。
- immediate recheck已经消失、轮询被中断、样本内多个denial、计时整数舍入、任一路径异常都需覆盖；固定总预算不能按每个PID重新分配100ms。

旧失败没有保存初始和错误后stat，也没有pidfd；稍后客户端rc0不能重建这些值。新合同是未来监控的有界退出证明，旧ENV_ABORT不变。本审查未执行任何验证；CPU合同与真实生命周期验证仍由根阶段负责。
