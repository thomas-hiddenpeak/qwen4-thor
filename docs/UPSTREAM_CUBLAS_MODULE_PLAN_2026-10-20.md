# 实际加载模块捕获预算（2026-10-20）

CUDA trace已捕获两种实际CUTLASS WMMA kernel名称，且所有投影输出不变。
静态cuBLAS库按名称/架构提取未找到指令体；不据此假定其实现。

复用冻结的upstream-projection-20261020/gpu-v1 executable与fixture。
一次进程，4次GEMV、1次BF16 batch、1次float batch，无模型加载/HTTP。
通过CUPTI RESOURCE_MODULE_LOADED只读复制包含目标名称的模块；
单模块上限256MiB，总512MiB，最多4模块。observer编译零警告。
不改launch参数、输入、累加器或生产文件。输出须逐字节等于原证据。

完整run_modules.py由gpu_exclusive.py持锁，输出
.q4t-work/upstream-cublas-modules-20261020/run-v1，lease-v1。
记录observer源码/库、驱动、输入摘要、模块及终态。失败即停止、不自动重试；
监控/竞争失败ENV_ABORT，仅停止自己的进程，保留现场。
模块取得后离线反汇编，核对实际名称及累加/epilogue指令。
若仍不足则保留未决，不把相同算法字段或名字直接当作累加路径相同。
