# SRV-02j1短进程保管修正与唯一复验

原冻结b224c567、native探针710f061e，4探针均本地exit0，15个
完整buffer共112MiB写读、14额度检查点均符合预期；然而原guard
终态为ENV_ABORT/75，driver最终被SIGINT终止。**原轮不能作为
环境有效的GPU通过**，不修改其exit.json/本地passed记录。

守护器第二次monitor报告compute PID2503164，但此时/proc已无该
PID。原`concurrent/started.json`保留同PID/start_ticks191930672、
ppid2503140与实际固定探针命令，0.264217秒exit0；它在两次轮询
间完成并被父进程wait回收，guard history尚无其身份。不能降低
guard的未知PID判定或假设轮询覆盖了短进程的整个生命周期。

## 唯一修正

新驱动用pidfd等待退出、waitid(WNOWAIT)读取结果，先保留子进程
的zombie身份和Popen对象，不立即waitpid回收。全部4探针结束后，
要求一个NVML查询开始时间晚于最后探针启动、且随后完成的无冲突
guard monitor；该完整/proc扫描窗口内4个zombie的PID/start_ticks/
ppid/pgid持续存在且可读，再回收它们。不是盲睡固定时长后视作安全。
等待最多8秒，查询/观察缺失或身份不一致立即失败；不改生产guard。

核心、native探针、原26 CPU/4驱动和全部依赖身份保持；不重新构建
CUDA探针、不扩大其功能/尺寸。不信任PID数字的历史白名单，不发送
任何非自有进程信号；父死亡绑定和阶段互斥继续保持。

## 新冻结预算（复验前）

新增一次独立完整GPU租约、相同4探针（每个15秒，总驱动90秒），
总计最多8次探针执行，其中原4个ENV_ABORT保持。这里重验整个短
进程组是因为身份保管贯穿四个子进程；不重验CPU分配核心或模型。
新驱动先做6项CPU验证，包含真正WNOWAIT/回收和超时只杀自有child，
以及旧观察/缺失查询/身份矛盾的拒绝。零模型、零HTTP、零长验。

入口`tools/serve_cuda_quota/correction.py`，原`plan.json`/execution/
lease/保持；修正独立`correction-plan.json`、execution-v2/、lease-v2/。
任一失败停止，不再增加尝试；最终同时核对驱动、guard、GPU清空、
原锁释放、默认pair/day保持。仍只接受额度核心，不代表serve已接线，
也不宣称物理RAM硬保护。
