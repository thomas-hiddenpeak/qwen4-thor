# SRV-16 独立前台控制器死亡验收

## 尚缺证明

SRV-14实际证明systemd环境下后端崩溃恢复和退避stop，SRV-15证明内存
保护退场。独立前台controller被SIGKILL时，父死通知能否带动实际GPU
后端、运行中请求和排队grammar全部退出仍只有CPU证据。本批对该
缺口做一次真实验收，不替换服务实现或默认ELF，也不把范围缩成空闲
进程退出。长期工业化目标保持。

## 互斥与独立监控

直接杀controller会同时终止其gpu_exclusive监控；所以验收进程是
controller的实际父进程，通过本机libc pidfd_getfd复制该自有子进程
**同一open-file-description**。不像重新open /proc/pid/fd，它保留原
flock。先核对boot/start/pidfd、直接父子关系、锁dev/inode与实际EX
所有权，再允许故障注入；本机CPU能力探针已证明原owner死后副本仍
排他、最后副本关闭才可再取得锁。没有修改ptrace策略或使用sudo。

生产controller经原包内gpu_exclusive.run_guard在模型加载前持锁并
监控，ready后复制同一锁且启动独立gpu_exclusive.inspect/NVML轮询；
两者重叠覆盖后再SIGKILLcontroller。独立副本直到完整自有清理、GPU
为空、停服观察完成才关闭，全阶段没有释放/重新申请锁的空档。
监控错误/竞争均ENV_ABORT，只清理自有身份，不杀竞争进程。

复制的FD不能单独证明worker/backend独立持锁；本轮分别检查它们的
真实FD和fdinfo，原独立存活命题复用CPU证据。控制器死后不以旧PGID
认领新进程，只保留已观察start身份与实际后代。原guard缺少exit.json
是预期死亡结果，不追写COMPLETED；完整custody结果独立保存。锁文件
中被杀controller最后的held元数据也不当作仍有flock，保留原件，以
实际flock及NVML判断最终状态。

验收专用exec前helper仅对**验收进程自身死亡**加parent-death通知，
随后exec未改的封存生产入口；不在生产包注入故障代码。外部验收进程
作subreaper，只回收已死孤儿，不在成功路径给worker/backend/grammar
补发终止信号。若需要辅助清理则保留现场并否决自然退场结论。

## 冻结矩阵与预算

- 原生产Python文件、默认C++211来源和d66f2bb9/42d7672a保持；复用
  SRV-15最终51有效CPU及guard30。新增8项CPU验证真实锁复制、错误
  身份/共享锁拒绝、monitor失败、PGID复用和错误终态拒绝。
- 一个前台controller/一个模型启动；准备新v2包，默认1GiBcritical门限、
  restart_limit=3、port18122，其他full-head MTP k3/seq1/8192配置不变。
  不做低内存测试，不声称验证真实1GiB边缘退场。
- 六次生成固定：普通JSON、严格JSON、真实MODEL资料读取/答案回填
  共四成功；随后普通SSE持续输出与严格JSON SSE排队各一次。
- 必须实际观察非空content、slot占用、queued1、grammar worker1且
  preparing0，两客户端仍活跃；只向本轮controller发SIGKILL。
- 原controller返回-9；worker由生产PDEATHSIG路径自然STOPPED并完整
  清理，后端drain0、两请求各一个server_shutdown完整SSE错误+DONE，
  不得有成功finish/usage终态，grammar共4个全部reaped。
- controller自己的terminal/guard exit缺失，status仍OWNER_ABSENT且
  cleanup_confirmed=false；不能把worker局部观察冒充控制器确认。
  本轮独立验证GPU、进程、监听退出；10秒无第二次attempt，不重放工具。
- 硬限1启动/6生成/1控制器死亡、显式控制64含清理8；监督GET保守188，
  合计不超过252（总观测预算260）。GPU窗口220秒、总600秒。
  超时/失败停止本轮，不自动增请求或重启寻找通过结果。

产物`.q4t-work/serve-controller-death-20261103/`，来源先commit再准备包
及计划。GPU测试结束释放原锁，保留SOAR持久mask，不安装systemd服务。
CPU先成组检查，只有具体失败才定向修复和补验。已有ELF、模型、模板、
数值证据复用；本项不提供性能或小时/天级长稳结论。
