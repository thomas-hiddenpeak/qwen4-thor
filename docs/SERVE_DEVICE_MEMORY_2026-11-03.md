# SRV-02f 设备资源失败与释放合同

2026-11-03，业务可靠性修复，当前待验收；默认仍为02e的f69982d5。
长期工业化目标保持active。代码为事实来源，限定文本服务与调度器
直接持有的设备缓冲区，不是整个模型的分配器重写或性能优化。

## 审计与修复范围

| 缺口 | 本阶段处理 | 边界 |
|---|---|---|
| 可选cudaMalloc把所有错误当OOM | 命名状态区分容量/非容量；仅明确OOM允许plain fallback | 不改变分配大小，不耗尽真实RAM来触发故障 |
| 必需请求缓冲分配失败只有泛化500 | JSON503 / SSE错误，code=device_memory_exhausted；释放已有资源 | GPU健康不因明确OOM自动关闭 |
| 其他设备错误仍服务 | gpu_unhealthy，ready关闭、锁内排空后停服；排空失败立即非零退出 | 不在原进程尝试恢复未知GPU状态 |
| cudaFree返回值忽略且成功终态过早 | 取走所有权后只调用一次；失败停服；终态与缓存发布前检查 | 不重试未知释放，不把API成功等同物理页立即归还 |
| MTP准备Status未分类却降级 | 在model锁内排空后停服 | 未改模型内部Status类型，不能证明可恢复OOM |
| 调度器析构失败仍退出0 | 明确非零退出，交由进程上下文回收剩余资源 | 不声称每个子对象析构均已检查 |

纯host策略在`device_memory.h`；CUDA适配在`cuda_memory_backend.h`。
故障注入仅在单独validation构建，生产构建关闭；模拟返回码不等于
真实物理OOM或设备损坏。`cudaMalloc`与`cudaFree`可能返回先前异步错误，
因此只有已知OOM且没有其他pending错误才消耗OOM指示并允许降级。
参考[NVIDIA CUDA13.3内存API](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/group__CUDART__MEMORY.html)
与[错误状态API](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/group__CUDART__ERROR.html)。

## 验收冻结范围

统一CPU批次：XGrammar OFF/ON host、必需合同、后端、配置、协议生成与
防遗漏inventory、驱动正反例。生产/validation独立构建，零警告；
锁内原生HTTP生命周期25项。真实运行前将源、ELF、驱动、计划和回退
pair冻结，完整阶段只持一把GPU锁；失败保留，只有具体失败定向修复。

计划9启动、16生成尝试、7负例、最多256控制请求，1800秒内完成含
安装与收尾（1500秒前进入安装）。所有监听loopback，MTP k3完整head。

1. 生产：普通JSON/SSE、严格JSON、JS真实资料工具调用及引用答案共5次。
2. validation可选prompt缓冲OOM：降级成功，同PID第二次正常MTP。
3. validation必需session缓冲OOM：SSE错误，同PID真实8192以上缓存
   seed/restore/fresh三次，核对命中及答案一致，不宣称数值等价。
4. 非容量分配错误JSON、早期prompt释放错误SSE、晚期draft释放错误
   SSE、未分类MTP准备失败JSON：各一次错误且进程非零退出。
5. 启动scheduler第二分配错误：已分配第一缓冲回收，启动非零且无ready。
6. 正常一轮后shutdown prefill释放错误：成功请求保留，进程非零退出。

HTTP失败不得有成功finish/usage；晚期失败允许已发送内容，但必须终止
为错误，客户端不得使用不完整答案或调用工具。每个运行只注入一次，
禁止自动重试。单独记录预期进程失败与ENV_ABORT，只有自己的scope
可停止。观测分RSS/memcg/global/CUDA，不相加，也不以memcg作硬限额。

## 后续仍需完成

- `chat_input.cpp`媒体路径的裸分配、释放和同步错误；媒体错误HTTP分类。
- 模型/MTP/视觉内部析构与启动失败清理、结构化Status错误传播。
- host内存分配失败时日志/错误响应可用性及系统资源耗尽保护。
- Thor整机物理RAM可证明保护；02e已证明cudaMalloc不能仅依赖memcg。
- 实际入口鉴权/TLS/多人策略、通知与新ELF长期运行、跨进程业务恢复。

通过本阶段也只关闭表中限定范围，不把新增capabilities当验收证据。
