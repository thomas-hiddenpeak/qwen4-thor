# NVML独立只读调研：故障边界与后续工作

2026-11-07，用户授权子代理调研；主代理复核官方文档及9月论坛回复。
本项未运行NVML、nvidia-smi、CUDA或模型，未修改驱动、服务与监控策略。
与[P0模型健康修复](SERVE_HEALTH_P0_2026-11-07.md)分开记录。

后续用户已授权并完成[错误分类与有界恢复CPU阶段](SERVE_NVML_RECOVERY_2026-11-07.md)；
下面保留调研当时的判断，真实驱动根因与GPU恢复仍未验收。

## 本机证据

10月20日同次现场记录了模型线程在物理页分配/规整路径，NVML线程
在`os_acquire_rwlock_read → rmapiLockAcquire`等待；模型随后成功启动
HTTP。因此监控超时本身不能证明模型OOM或执行失败。尚未追踪到
准确锁持有者，不能把相关等待直接作为完整因果证明。参见
[历史诊断](NVML_LOAD_DIAGNOSIS_2026-10-20.md)及
[原始等待栈](evidence/nvml-load-20261020/wait-witnesses.json)。

11月7日捕获的高阶分配失败仍存在kvmalloc回退，缺最终分配返回与
同次NVML worker等待栈；不能断定全部故障与10月20日同根因，也
不能从MemAvailable/CMA单点值推导新的准入阈值。参见
[环境诊断限制（保留的研究来源）](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7e6562136cf7581fdcfd6ff74a7a2b6e3d0ae7f9/docs/VISION_ROWMAX_ENV_DIAGNOSIS_2026-11-07.md)。

## 官方线索与限制

当前CUDA for Tegra文档明确Thor及后续L4T Tegra支持NVML，早期
Jetson不支持NVML的结论不能直接套用。
[NVIDIA文档](https://docs.nvidia.com/cuda/cuda-for-tegra-appnote/#cuda-features-not-supported-on-tegra)。

2026年9月的Thor/R39.2论坛报告也出现NVML进程在RM锁上等待。
NVIDIA支持人员建议尝试nvgpu firmware补丁；后续用户仍报告监控
读取异常，帖子没有证明本项目环境的故障已修复。这是待核对的
线索，不构成直接升级或套补丁的依据。
[原帖及官方回复](https://forums.developer.nvidia.com/t/jetson-agx-thor-devfreq-wq-deadlock-with-rmapilockacquire-causes-gpu-tasks-to-hang-in-d-state-jetpack-7-2/383291)。

NVML错误枚举明确区分超时、库/驱动版本不匹配、设备不可访问和
需要reset等情况。应保留错误类别，不能一律当作可暂时忽略的查询
故障；厂商错误码仍不是重置设备的自动授权。
[官方错误定义](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceEnums.html)。

## 工程判断与后续有界范围

当前`nvml_process_query.py`已记录`nvml_code`，`telemetry.py`仍将
全部ERROR同类退避；重试耗尽后的suspended也没有本代次恢复入口。
建议另设CPU阶段处理以下两项，本轮仅登记，未实现：

1. 区分外部期限TIMEOUT、NVML自报错误、权限/库版本配置错误、明确
   设备故障及协议损坏；成功、未知和历史缺口分别记录。
2. 监控暂停后仅允许显式、有冷却与总预算的重新观测；旧worker确认
   回收才可创建下一次查询。恢复监控不隐式启动或重启模型。

保留现有子进程隔离、期限与回收规则。无法回收的D-state查询不得
通过继续派生进程绕过；未知PID集合不能解释为GPU空闲。
应用进度和/proc/meminfo可补充业务与内存证据，但不是全机GPU
context所有权证明，不能替代严格验收监控。

本轮不再主动复现厂商故障。以后若获授权恢复GPU工作，可在一次
自然故障中复用已有等待栈观察器，同时保留查询边界、模型/worker
栈与内核记录；没有新证据仍维持根因未决，不延长超时或追加采样。
