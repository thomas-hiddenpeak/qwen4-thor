# 当前Thor整机物理RAM硬上限：有限可行性结论

2026-11-05，结论为 **NO_GO_CURRENT_CONTRACT**。对当前内核/驱动和本项目
实现，不将“全部物理RAM可强制限制”列为已交付能力。本次审查结束该
交付关卡；不是对未来硬件/驱动或所有可能机制的不可能性证明，也不
阻塞明确容量配置下的受控服务。没有新增GPU探针、压力或OOM测试。

## 2026-11-25 平台复核

本机运行 Jetson Linux R39.2.1、内核 6.8.12-1021-tegra 和 CUDA 13.3.0。cgroup v2 根层级列出了 cpuset、cpu、io、memory、hugetlb 和 pids；其中没有 dmem 或 misc。NVIDIA 的 MPS v3 设备内存分区要求 CUDA 13.4 以上（含 13.4）和非 MIG 设备，托管内存还有单独的计量条件。Jetson 示例中的 MIG 配置列出 0 GB 的专用内存。上述资料给出了后续板卡升级时可验证的候选机制；本机的 CUDA 申请额度、memcg 和全局水位仍应分别报告。[本机只读记录](evidence/serve-cold-driver-cpu-20261112/rev484-preflight.json) · [NVIDIA MPS 内存分区](https://docs.nvidia.com/deploy/mps/latest/mpsv3-memory-partitioning.html) · [Jetson R39.2.1 MIG](https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/MiG.html)

## 已有反例与代码覆盖

[SRV-02e的实机分配](SERVE_STARTUP_MEMORY_RESULT_2026-11-03.md)已经给出
充分反例：256MiB cudaMalloc实际触页后，全局可用内存减少约257MiB，
memcg增量约−0.008至0MiB。不能把host MemoryMax推导为全部CUDA物理
内存上限；其他三个分配类别的正例也只覆盖当时的有限条件。

| 机制及代码入口 | 实际约束 | 未覆盖的硬保证 |
|---|---|---|
| `src/runtime/cuda_allocation_quota.cpp`、`CMakeLists.txt`链接wrap列表 | 项目已链接的device/pinned/managed/async申请字节及生命周期账本 | DSO/driver内部申请、池物理保留、host堆及其他进程 |
| `tools/serve_service/resources.py` | 明确owned payload的host memcg与pids，control另有预算 | 上述未被该域完整计量的CUDA；域外物理占用 |
| 启动容量检查及`include/q4t/server/cuda_quota_policy.h` | 启动前容量估算、按MemTotal比例解析申请额度 | 物理预留、动态外部竞争或峰值同时性 |
| `tools/serve_service/memory.py` | 每秒全局可用内存观察，持续低水位/未知触发owned进程退场 | 两次观察之间的快速耗尽、硬实时退场或不可中断任务退出 |

本机host、device、managed共用DRAM；CPU迁移不是容量卸载。文件缓存、
staging、库内部缓冲都要考虑，但RSS、memcg、CUDA账和MemAvailable有
重叠与遗漏，不能直接求和或将未观测项置零。已验CUDA额度仍有实际
约束作用，不能把其“请求字节额度”名称替换成物理峰值保证。

## 可交付合同与重新开启条件

本轮采用固定单槽8192/prefill2048/MTP3、明确客户端请求预算、可选
host high/max/pids、现有CUDA账和全机水位退场的组合。各机制分别
记录配置和观测；短验成功也不自动接受其他上下文、媒体、卸载配置或
整机硬上限。组合接受见当前阶段结果，本文不预先宣布它通过。

只有出现能覆盖CUDA/driver物理驻留的可强制机制，或业务明确需要
不同进程/设备隔离边界时才重新立项。届时先给出计量归属、完整覆盖
清单、并发及快速分配的强制失败路径，再冻结有限验证；不靠长稳未
出OOM、重复相同memcg探针或无限降低水位来证明硬上限。
