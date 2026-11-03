# CUDA申请额度核心

本目录验证`include/q4t/runtime/allocation_quota.h`，当前**尚未接入
serve默认程序**。它是未来入口的申请字节额度，不是物理RAM限额。
合同、预算与下一步见[SRV-02j1](../../docs/SERVE_CUDA_QUOTA_CORE_2026-11-04.md)。

开发验证只有下列显式入口，没有timer、常驻服务或自动重试：

```bash
python3 -B tools/serve_cuda_quota/stage.py build
python3 -B tools/serve_cuda_quota/stage.py cpu
# 实现/验证工具形成阶段提交后，冻结GPU身份；四个小探针，不加载模型。
python3 -B tools/serve_cuda_quota/stage.py prepare
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/serve-cuda-quota-core-20261104/lease -- \
  /usr/bin/python3 -B tools/serve_cuda_quota/stage.py execute
```

输出目录只创建一次，失败保留，禁止删掉原件后重试。`check`只读
检查源/默认pair/day身份，不调用CUDA、模型或HTTP。CPU可按case名
定向运行`.q4t-work/serve-cuda-quota-core-20261104/build-v2/quota_host_contracts`
，重验必须另存记录，不覆盖原CPU结果。

账本采用固定容量表，申请前同时预占表项和字节；后端失败且没有
返回指针才允许退额。未知所有权保留并拒绝新申请，已知的其他live
资源可以清理。核心析构不进行CUDA调用，不自动释放或重放未知项。
请求字节为正数；CUDA零大小/null free兼容语义留给后续边界适配。

接线必须遵守：所有实际物理申请（含startup owner的Backend）只
记一次，预留指针交付不重复记账；`queue=0`为device同步，非零
queue token须按stream生命周期唯一，不能直接使用会复用的原始
handle。异步free保留pending，只有先capture再成功同步覆盖到的
账目才退额。event/stream销毁、后发free或失败同步均不能冒充完成。
async分配要求async free加同步，不把其cudaFree当作隐式完成保证。

后续02j2还需完成真实wrapper接线、stream代次管理、零大小兼容、
CLI/config/metrics、明确错误归类和真实HTTP/工具验收；不能因为本
核心通过就宣称所有CUDA分配或serve受到限制。driver/库内部、普通
host堆、文件缓存和池保留是独立边界，不计入本账本。

初轮25 CPU/4驱动通过的`cpu/`和`build/`保持。随后定向复现释放
已到达CUDA、尚未发布账本时的合法地址复用误判，原失败保存在
`publication-repro/`；修正后26 CPU/驱动验证写入`cpu-v2/`和
`build-v2/`，不覆盖初轮。GPU次数和大小预算保持。

首轮4 CUDA探针虽本地通过，外层guard由于短进程被过早回收而
ENV_ABORT；原件不追改。当前按[保管修正合同](../../docs/SERVE_CUDA_QUOTA_CORRECTION_2026-11-04.md)
执行唯一额外4探针，沿用相同native探针和CPU核心身份。新入口如下，
仍是显式操作，不在旧目录重跑：

```bash
python3 -B tools/serve_cuda_quota/correction.py cpu
python3 -B tools/serve_cuda_quota/correction.py prepare
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/serve-cuda-quota-core-20261104/lease-v2 -- \
  /usr/bin/python3 -B tools/serve_cuda_quota/correction.py execute
```
