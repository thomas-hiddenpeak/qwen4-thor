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
  compiler entry、编译超限及`wait4.ru_maxrss`，不把它与CUDA数值相加。

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
