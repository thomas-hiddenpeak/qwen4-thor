# SRV-02j2 生产CUDA申请额度

接入已验固定额度核心，实施/验证范围见
[本阶段合同](../../docs/SERVE_CUDA_QUOTA_INTEGRATION_2026-11-04.md)。
这是业务可靠性工作，没有性能优化或整机物理RAM硬限额声明。

生产参数`--cuda-allocation-limit-mib auto|0|N`默认auto，启动时取
mem_fraction×Linux MemTotal作为请求字节上限。正整数按MiB显式指定，
0明确关闭；`--no-budget`不关闭该额度。额度每进程只能配置一次，
重配须启动新进程。实验性`Q4T_STARTUP_RESOURCE_PLAN=ON`构建不提供
此生产wrapper，必须显式传0；普通生产构建保持该开关OFF。

覆盖GNU link边界内的cudaMalloc、cudaMallocManaged、cudaHostAlloc、
cudaMallocHost、cudaMallocAsync。启动预留物理申请计一次，交付给
模型不再加计；静态进程缓存继续计额。异步free提交后仍占pending，
只在匹配流代次或device同步成功后退额；event完成/stream销毁不退。
固定16384分配项/256显式stream项，表满或字节不足先于CUDA拒绝。

`/v1/config`公开有效上限、来源和排除项；`/metrics`中的
`q4t_cuda_quota_*`区分reserved/live/releasing/pending/unknown与拒绝。
可恢复的serve缓冲拒绝返回503/`cuda_allocation_quota_exhausted`；
其他CUDA失败或内部部分执行失败仍按原合同停止服务。退出日志中的
剩余live可能属于既有进程缓存；没有将它解释为物理未回收字节。

DSO内部、host堆、driver、文件缓存与CUDA池保留不在额度覆盖内。
Thor host/device共用DRAM，pinned计额不代表CPU容量卸载；已有
MemAvailable准入/托管退场保护继续独立工作。

构建/CPU产物只进入`.q4t-work/serve-cuda-quota-integration-20261104`，
原失败记录不覆盖。GPU执行前要求源已提交、CPU通过和固定包完成；
`stage.py launch`自动持有一把完整阶段租约，执行6小型原生模式、
25原生HTTP生命周期检查、1次低额度启动拒绝、2成功模型启动与7生成。
普通JSON/SSE、严格JSON、真实资料工具及引用、两个并发客户端都在
固定600秒预算内。没有自动重试，没有小时/day执行或vLLM恢复。

通过后复用既有default pair事务封存回退、安装同一ELF并核对版本；
外层guard完成后再finalize。长验由用户开展，旧day包保持独立，
新默认的交接必须重新绑定来源，不能沿用旧版本的验收结论。
