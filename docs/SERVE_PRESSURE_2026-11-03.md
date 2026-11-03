# Serve 同进程压力恢复与持续混合业务合同

任务为业务可靠性验证，接续SRV-02c，长期工业化目标保持active。
先成组实现/CPU核验，再冻结源码与原预算；已通过且身份不变的运行时
证据复用。只修复具体失败后重验受影响项，不自动扩样或改接受标准。

## 身份与安全边界

使用当前安装的`build/q4t` 84d16983 / sidecar c5203331，protocol
aeb66adc；运行时、模型、模板、grammar均不改动，无需重新构建。
全程同一GPU互斥锁，loopback18106，单槽8192/prefill2048、inflight8/
queued2、完整head、普通MTP k3和严格plain。测试显式floor30720MiB、
margin512MiB；默认配置未改变。初始可用内存必须处于31–37GiB，
否则停止，不能临时调门限以取得通过。

模型置于唯一token所有的systemd scope，保持调用者祖先关系；
MemoryMax=infinity、MemorySwapMax=0，客户端/压力子进程在scope外。
不改共享session/cgroup、不碰已有服务，不清全机缓存。
scope只是核算视图，不代表整机硬限额。已有
[独立CUDA核算探针](OFFLOAD_RAM_PROTOCOL_2026-10-02.md)证明该测试路径
存在device漏计；旧探针不是本轮驱动版本所有分配的完整证明。

专用匿名CPU压力块每次128MiB，最多8192MiB，实际触页；不是专家
容量offload。每次分配前和持续100ms观察保留MemAvailable20GiB、
MemFree12GiB，下一块也计入余量；缺失/异常即退出并释放自己的内存。
stdin EOF、父pidfd退出、10秒命令空闲或120秒总寿命均释放；独立
CPU检查实际128MiB、EOF、父退出和scope归属/回收。无需实际触发OOM。

## 固定业务与接受条件

计划326生成尝试、4负例、1模型启动、65预检。硬限330/6/2/1800秒，
含首次lease至finalize；最后60秒留给退出。没有自动重试，预算不是
允许为了通过重复全组的理由。全组保持同一模型PID/start ticks。

1. 普通SSE真实content开始（1024token有界连续数字任务）后提交严格
   SSE，确认实际queued=1、worker=1。分配至低于floor512MiB：排队
   严格请求必须一个memory_pressure错误与一个DONE，worker/queue回收；
   原运行请求在门关闭后继续输出真实content。
2. 三次只送头的chat/chat/preflight均503 memory_pressure，body不发送；
   不据此区分JSON/SSE。ready503而live/health200，未知取消404，正确
   凭据取消holder成功且不报告完整成功；owners归零、slot空闲、计数一致。
3. 分块释放到floor与floor+512MiB之间，实际指标落入区间且ready仍503；
   全部释放后同一PID恢复ready200。普通710003、严格`{"ok":true}`、真实
   MODEL.md第1–24行工具读取与引用回填，共4次成功生成加1预检。
4. 连续64轮，每轮普通JSON/SSE和一个严格请求并发，再做一次真实工具
   读取/答案回填；严格JSON/SSE交替，每轮一次预检/控制检查。共320
   成功生成、64次真实工具执行。任务结果、参数/调用ID、引用和完整
   协议都必须通过；无固定空闲间隔，控制handler退出仅留50ms。
5. 每轮归零所有owner/队列、worker started=reaped、slot全部free。
   前8轮为allocator预热基准，后56轮每个静止端点不得超过前8轮最大
   值：parent RSS+64MiB、scope anon+128MiB、FD+2、threads+2。小额
   FD/thread余量容纳刚结束的控制handler；这是本负载工程接受包络，
   不是任意任务/长期泄漏保证，不能事后调整或用线性拟合掩盖超限。
6. 每秒记录process身份/RSS、scope current/peak/stat/events、全局内存，
   非原子、重叠计量分列，不相加。丢观测/身份变化是ENV_ABORT，owned
   scope OOM拒绝接受。最后正常排空退出、独占scope移除、GPU释放。

预期服务生成账324成功、1主动取消、1排队拒绝；3个body前拒绝不属于
生成账。CPU程序/驱动测试日志与实际wire均归档，失败也保留。当前
发布pair完全复用，不为测试替换binary/sidecar；通过后结果单独挂到
工业化验收账，不声称安装新版本。

## 仍未证明

固定轮数运行按实际持续时间报告，不称小时/天级长稳；没有触发物理
OOM、没有建立整机RAM硬保护，也没有给启动加载提供准入保证。GPU
guard采样不能排除短于间隔的干扰。实际网关/鉴权、多人公平与租户
配额、跨进程业务恢复、发布/回退演练仍是独立工作项。
