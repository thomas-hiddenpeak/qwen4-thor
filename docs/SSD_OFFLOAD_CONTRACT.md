# Thor统一内存下的SSD offload合同

2026-10-21按用户明确要求建立。本文约束当前与后续MoE expert offload
的设计、文档和验收；历史报告保留原始事实，不作为与本文冲突的执行计划。

## 唯一容量后备是SSD

本项目的MoE专家**只向SSD做容量卸载**。Thor的CPU和集成GPU共用
物理DRAM；host、device、managed allocation都来自该内存。把专家从
device allocation移到CPU内存，仍然是RAM驻留，不构成容量卸载。
这是[CUDA 13.3 Tegra内存说明](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-for-tegra-appnote/index.html#memory-management)
与本项目目标共同确定的边界。

“与直接载入内存没有区别”在这里指**没有移出物理RAM**；不代表不同
分配方式的访问、缓存、拷贝、同步或性能完全相同。统一物理内存也不
自动消除现有H2D/D2H或重复副本。

专家权重保存在只读SSD checkpoint，需要时读入有界工作集供GPU计算。
当前实现不把GPU驱逐解释为写回SSD：原始权重已在SSD，设备槽可直接
丢弃或放入既有有界DRAM镜像。修改原模型目录不属于任何优化出口。

## 允许的中转与禁止的容量表述

| 对象或说法 | 当前含义与约束 |
|---|---|
| SSD offload | SSD承担不常驻DRAM的专家容量；驻留工作集、缓存与中转受总RAM目标约束 |
| device专家槽位 | GPU计算使用的DRAM工作集；减少槽位本身不证明整机RAM下降 |
| pinned staging / host L2 / mirror | DRAM中的中转或缓存；必须计入容量，不能称为另一个offload层 |
| Linux文件页缓存 | SSD文件内容的DRAM副本；可回收不等于已释放，更不等于容量免费 |
| CPU offload / GPU→CPU卸载 | 不用作本项目容量方案；只能在介绍独立显存机器的外部机制时限定平台 |
| NVMe→host→GPU | 数据路径，可以有多个buffer；不是三个独立的物理容量池 |
| SSD逻辑读取 | 应用向checkpoint请求的字节，可能命中页缓存；不等于实际SSD读取 |
| GDS / cuFile | 候选SSD读取方式；是否走原生直达须另证，不新增容量层 |

有界主机中转可以存在，因为文件读取、scale布局转换及异步上传需要
生命周期明确的缓冲。若为SSD服务采用小规模缓存，必须有明确预算，
同时报告其DRAM成本和实际SSD/E2E效果；缓存命中率不是容量交付。
不得用全量CPU驻留、持续大页缓存或未入账的库内部buffer掩盖SSD供给。

此前提出的跨层共享主机缓存**已经搁置，仅保留离线研究记录**，不作为
当前候选队列或自动恢复任务。本文不新增缓存实现，也不追溯宣布既有
host池已满足未来低RAM目标。

## 当前代码与未闭合项

- `serve --moe-resident-slots 0`是当前默认，全部专家device驻留；非零
  选择按需槽位实现，允许C=512。只有实际C<512的层才减少device
  专家槽容量，槽位减少也不证明统一RAM收益。全驻留默认健康不等于
  offload验收。
- `src/quant/moe_residency.cpp`当前使用host staging、每层L2与mirror；
  `src/io/safetensors.cpp`通过buffered pread/preadv读文件，随后host
  合并/swizzle scale，再H2D。文件存在SSD不等于每次miss均触发SSD。
- 当前非零槽位路径的L2默认128槽/层、mirror默认8槽；48层静态pinned
  payload约21.19 GB。受测L2=16/mirror=8约3.58 GB。二者都未包含
  文件页缓存等成本，也不是完整物理峰值；不能混淆默认与实验配置。
- 启动资源预留修复的是已验收配置的分配顺序与所有权，不是更低RAM
  预算或SSD持续供给的证明。实际启动曾保留大量专家页缓存。

代码与历史证据详见[前提对齐审计](SSD_OFFLOAD_ALIGNMENT_AUDIT_2026-10-21.md)。
PLE另有io_uring页池路径；同样遵守统一RAM计量，不能当成现成GDS实现。

## 容量与读取验收

1. **先定义资源条件**：冻结device工作集、host staging/L2/mirror、
   文件缓存、库内部缓冲及其他模型状态的预算与测量范围。进程RSS、
   CUDA/NVML、memcg和文件缓存有重叠/漏计，不能把独立峰值相加。
2. **持续记录缓存**：冷起点后加载会重新暖缓存；只清一次缓存或
   给memcg设置限额，不能证明持续低RAM或限制了组外缓存与CUDA分配。
   总量未闭合就明确未知，不冒称达到某个整机RAM上限。
3. **分开记录逻辑与物理I/O**：现有`nvme_mb`、`nvme_read_bytes`
   是应用逻辑计数。PID/块设备读取也可能含PLE、元数据、预读和背景
   I/O；必须限定归属。未知不能当0，父盘与分区不能重复累加。
4. **同时满足正确性与服务条件**：SSD短读、错误、取消、槽位复用、
   I/O完成和GPU消费的所有权均需有效；缓存或I/O策略不能改变权重
   数值合同。容量完成和性能优化结论分别记录。
5. **遵守任务分类与有界验收**：可靠性按针对性有界合同；性能优化
   保持HTTP E2E优先及适用完整对照。不为统计不足无限补测，不用
   gdsio、bench或单项带宽替代项目E2E。详见[EVALUATION](EVALUATION.md)。

## 文档维护与下一步

AGENTS、README、架构、评估规范及当前状态均按本文解释offload。
历史日志、封存证据和旧实验结论不重写；历史报告中的CPU缓存计划或
“三级存储”不自动成为当前路线。外部独立显卡项目的CPU卸载只能
借鉴调度/生命周期，不能直接继承其容量收益。

下一步围绕SSD工作集预算、持续文件缓存与真实读取归属收敛。NVIDIA
直读的当前可用性、适配成本及推进条件见
[GDS只读评估](SSD_DIRECT_IO_EVALUATION_2026-10-21.md)。
