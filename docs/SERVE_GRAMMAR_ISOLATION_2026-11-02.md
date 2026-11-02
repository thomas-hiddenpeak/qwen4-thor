# serve 编译与匹配隔离合同

起点：`48e0682`，默认 f03ee0d4/a1e9d24e。业务资源治理，非性能优化。
代码是事实来源，本文先冻结边界，结果在下方追加。上游固定版本无
编译抢占接口，不修改只读依赖。

## 实现边界

- 每个受约束请求 exec 同一个正在运行的 ELF（`/proc/self/exe`），
  在 CLI/模型初始化前进入 CPU worker；没有 CUDA 运行时 fork 后计算。
  单租约保留至语法对象和输出清理，普通请求不占此租约。
- compiler、tokenizer info、matcher 全部位于 worker；主进程只发送
  最多32MiB的私有词表/结构标签帧，接收与词表严格等长的掩码。
  不反序列化 XGrammar 对象，不允许错误时退回未约束输出。
- worker 限2GiB `RLIMIT_AS`，进入编译前核对已加载映射不超限；
  栈8MiB、fd32、core0、1编译线程、关闭cache。编译结果原64MiB近似
  storage门保留，父进程原32MiB词表门保留，传输副本也至多32MiB。
  地址空间覆盖工作进程映射页的上界，**不是整机/主服务RAM上限**，
  不包含内核socket/页表等，也不宣称设置了独立可执行的RSS限额。
- 主进程用单调时钟，10秒覆盖词表准备后的剩余编译窗口、exec、IPC、
  tokenizer构造与初次mask；每次后续匹配2秒。每个等待最多10ms检查
  请求取消/deadline/断连/shutdown。工作进程有11秒/3秒备用alarm。
  OS调度、exec内核路径与kill后的reap不是硬实时系统，响应不承诺
  精确到某毫秒；超限停止自有worker，reap后才归还资源所有权。
- 固定fd3私有socket；0/1/2定向`/dev/null`，其余fd关闭，不继承服务
  socket/模型fd。parent-death signal防孤儿；pidfd只操作本请求子进程。
  同一ELF保证协议/上游版本一致，不增加可错配的外部可执行文件。
- 编译超时503/constraint_compile_timeout，内存门503/
  constraint_resource_exhausted，异常worker502/constraint_worker_failed。
  请求取消仍409及既有原因；生成阶段失败保持既有错误终态，无成功finish。
  不自动重试。capabilities暴露这些限制；metrics记录start/reap/active、
  compiler entry、编译超限及工作进程报告的post-exec VmHWM。
  `wait4.ru_maxrss`另列lifetime指标：它包含exec前继承的mm峰值，不能
  与worker地址空间上限直接比较或当作新增RAM；VmHWM报告可能漏掉
  突然kill前最后一段增长，零报告视为未知，不冒充完整峰值。

Linux语义依据：[getrlimit](https://man7.org/linux/man-pages/man2/getrlimit.2.html)、
[parent death signal](https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html)、
[pidfd](https://man7.org/linux/man-pages/man2/pidfd_open.2.html)。Linux的
RLIMIT_RSS不能提供此处所需的普遍RSS限制；不用采样超限冒充硬内存门。

## 冻结验收

CPU：host ON/OFF、Thor必需和真实后端门、进程超时/取消/短读/断写/
地址空间拒绝/fd关闭/回收与无泄漏；真实XGrammar编译进入后取消、
低内存失败后恢复；原掩码数值/UTF-8/tool/no-fallback合同必须通过。
只扩充直接受影响测试；复用未变模板/输入/数值证据。

单一GPU guard下至多3启动、12请求尝试、6拒绝、30分钟：保留上组
严格与普通混合/显式取消/deadline/shutdown、真实只读文档工具回填，
增加编译阶段取消，核对worker回收与RSS实测，最后默认普通MTP和严格
SSE健康。失败留证、只重验受影响项，不扩大压力或5档性能测试。

CPU已通过：host ON164/OFF157、Thor必需123＋后端7、驱动7/回退6，
三组构建零警告。真实XGrammar编译进入后取消和低内存拒绝/恢复通过。
当前尚未进行HTTP/GPU验收或替换默认。真实清单实际10次请求尝试/
1个过载拒绝/2启动，其中一次通过pidfd暂停本阶段拥有的worker，验证
编译10秒超时/回收，不属于真实生产故障注入接口。


## 首轮失败与针对性修复

首轮guard COMMAND_FAILED（非ENV_ABORT），1尝试/1启动、38.61秒。
本机内核没有`/proc/PID/task/TID/children`，驱动识别阶段失败；server
正常shutdown，worker回收，未安装候选。现场保留在原阶段execution。
发现wait4原始ru_maxrss为4093353984字节，包含exec前父mm峰值；据此
修正计量为child报告的post-exec VmHWM和原始lifetime rusage分列。
[proc status](https://man7.org/linux/man-pages/man5/proc_pid_status.5.html)定义
VmHWM；它是进程当前映射的历史RSS观察，并非memcg物理计量。

驱动改为读取proc status按已创建server的PPid筛选，再核对同ELF、
命令和父身份后使用pidfd；仅在worker确认rlimit安装后暂停它。
修复阶段用`.q4t-work/grammar-isolation-20261102-repair/`，沿用首轮
started与30分钟总窗口，剩余至多2启动/11尝试，不扩大原预算。

## 第二轮观察时序修复与收尾范围

修复候选c8c412c4在55.13秒内完成7尝试/1拒绝/1启动，编译timeout
10.008秒、编译取消0.067秒、混合与真实工具闭环已通过。末尾snapshot
遇到响应已送完但最后worker尚在reap的窗口，错误判泄漏；随后日志
明确最后worker已回收、server正常关闭。修复驱动等待资源清理，保留
该失败；不重复同身份已通过用例。

最后一次启动只做普通/JSON schema健康和active shutdown，共3请求；
累计包含第一轮失败将为11尝试/1拒绝/3启动，仍原30分钟窗口。通过
后安装同SHA ELF，复用刚完成的同ELF HTTP健康；安装路径再验证CLI和
私有worker限额握手，无新模型请求。运行时/ad69ce4和候选c8c412c4
不变，源码与依赖hash绑定；有界收尾见tools/grammar_isolation/finish.py。
