# FI02解码进程总IO预算CPU接受

2026-11-10。7新增与11直接受影响用例，18/18通过，0.765秒。首次
host构建因测试线程忽略write返回值被Werror拒绝，原件保留；夹具改为
检查实际写入，最终host/引擎零警告。host`9acabc4b`、引擎`81c9de81`，
执行前完整SHA已记录。0模型/0GPU查询，默认`cdb5ced9`保持STOPPED。

使用[Linux seccomp用户通知接口](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)：
隔离子进程把内核listener通过既有认证fd3交给唯一父读取者，随后
关闭自己的副本。只根据不可变标量参数预先扣减累计write/pwrite字节、
ftruncate声明长度、创建文件/目录和线程次数；失败、短写、截断和删除
不退款。没有读取可并发修改的路径/iovec指针，实际路径访问仍由后续
完整Landlock/syscall策略决定，不能把预算组件当完整沙箱。

父进程的Transfer、CollectOutput和EOF后的退出等待均处理通知，
仍使用同一个owned PID/pidfd、截止、取消、回收计数和未知退出占槽。
通知超限在系统调用执行前停止该进程，部分输出不发布。测试实际确认
第二次超额写入/增长没有发生、第三个文件/线程未创建、完整大块输入/
输出后仍能处理清理写入；假listener/重复绑定、连续通知中的取消和
期限、原有未知退出/文件解析均通过。

Linux6.8源码核对单读取者的请求计数/撤回规则；不假设O_NONBLOCK能
让通知ioctl不阻塞。每次仅在poll可读后领取通知，无并发读取者消耗它；
撤回按ENOENT处理。源码参考为
[v6.8 seccomp.c](https://github.com/torvalds/linux/blob/v6.8/kernel/seccomp.c)，
本项目使用系统接口独立实现，没有复制内核或第三方解码器源码。

仅接受本目录冻结的组件合同。完整旧格式转换还须使用Landlock、禁止
旁路写入的syscall策略、私有临时目录/清理和实际文档解码；这些不能据
本次通过视为已验。Legacy/Apple/Google及FI03—FI05全部API/模型/历史/
业务与总计划继续，尚无默认发行或main合并。
