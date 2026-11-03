# Thor 原生 FP4/FP8 转换及 QMUL4 等价组合实机核验

日期：2026-11-03，23:52:56–23:53:10 CST。任务为指令能力与数值
等价性研究，不是性能优化；未修改生产 kernel 或默认程序。

## 结论

**Thor 有可用的原生 FP4/FP8 转换硬件，并且两条原生指令组合已经
通过实机逐位数值核对。** 它们实现：

`E2M1 FP4 × E4M3 scale → E4M3 FP8`，采用 nearest-even 与 satfinite。

一条组合使用 FP16 中间值，另一条使用 BF16 中间值；两者均由原生
`F2FP` 转换和 packed 乘法执行，无查表、无 CPU 代算、无 QMUL4。
在全部有限标量输入组合及四个 lane 覆盖上，两条路径与精确参考逐位
一致，且两者完整输出相同。

“Thor 的某条等价单指令采用不同编码”仍然不能被上一轮非法指令
实验排除；本轮没有找到这种 Thor 专用编码。本轮八个已知原生探针
在 `sm_100a` 和 `sm_110a` 上的完整 `.text` 字节完全相同，说明这些
转换/乘法使用相同编码；此结果不能外推为所有指令都相同。

前一轮的 [QMUL4 非法指令证据](CUDA134_QMUL4_THOR_2026-11-03.md)
保持有效：它否定的是所测 QMUL4 编码在当前 Thor/驱动上的可执行性，
不否定本轮已证明可执行的转换组合。

## 公开指令与实际 SASS

[PTX ISA 9.4 的 cvt 说明](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#data-movement-and-conversion-instructions-cvt)
列出 Thor 对 FP4→FP16 及 FP4/FP8→BF16 等转换的支持；FP8 与 FP16
之间的转换覆盖 sm_89 及后续架构。本机 CUDA 13.3 的 `cuda_fp4.hpp`
与 `cuda_fp8.hpp` 也使用对应 PTX。以下结果来自隔离 CUDA 13.4.92
为 `sm_110a` 实际生成的 SASS，并已在本机执行：

| 操作 | PTX | 生成的核心 SASS |
|---|---|---|
| FP4→FP16×2 | `cvt.rn.f16x2.e2m1x2` | `F2FP.F16.E2M1.UNPACK_B` |
| FP8→FP16×2 | `cvt.rn.f16x2.e4m3x2` | `F2FP.F16.E4M3.UNPACK_B` |
| FP16×2→FP8×2 | `cvt.rn.satfinite.e4m3x2.f16x2` | `F2FP.SATFINITE.E4M3.F16.UNPACK_B_MERGE_C` |
| FP16×2→FP4×2 | `cvt.rn.satfinite.e2m1x2.f16x2` | `F2FP.SATFINITE.E2M1.F16.UNPACK_B_MERGE_C` |
| FP4/FP8→BF16×2 | `cvt.rn.bf16x2.e2m1x2` / `e4m3x2` | `F2FP.BF16.E2M1/E4M3.UNPACK_B` |
| BF16×2→FP8×2 | `cvt.rn.satfinite.e4m3x2.bf16x2` | `F2FP.SATFINITE.E4M3.BF16.UNPACK_B_MERGE_C` |
| packed 乘法 | `mul.rn.f16x2` / `mul.rn.bf16x2` | `HMUL2` / `HFMA2`，BF16 带 `.BF16_V2` |

编译器把其中一条乘法映射为加数 `-RZ` 的 `HFMA2`；本轮逐位检查
包括符号零。BF16 解码 SASS 的附加常量为 `0x7f7f`，即默认单位
UE8M0 scale；这里没有将其误解为可直接应用任意 E4M3 scale。

两元素 FP16 路径的核心 PTX 为：

```ptx
.reg .b8  fp4_pair;
.reg .b16 scale_pair, out_pair;
.reg .b32 w2, s2, product2;

cvt.rn.f16x2.e2m1x2 w2, fp4_pair;
cvt.rn.f16x2.e4m3x2 s2, scale_pair;
mul.rn.f16x2 product2, w2, s2;
cvt.rn.satfinite.e4m3x2.f16x2 out_pair, product2;
```

四元素执行两组。实际组合的算术/转换核心为 **6 条 F2FP + 2 条
packed 乘法，共 8 条**，另有寄存器打包、寻址、加载和存储。
这属于功能等价，不能从指令条数推导吞吐比；本轮没有测性能。

公开 PTX 的直接 `cvt.rn.satfinite.e4m3x2.e2m1x2` 试编译，在
sm_100a 和 sm_110a 均报 `Unexpected instruction types`、rc255。
这是该 PTX 类型组合不合法，不能作为 Thor 缺少某种隐藏编码的证据。
[新 packed mul 的公开目标范围](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#alternate-floating-point-instructions-mul)
仍只列 sm_100a/103a；上一轮实编译与实机失败记录直接复用。

## 等价性的数值理由

有限 E2M1 有效精度不超过 2 bit，有限 E4M3 不超过 4 bit，乘积
最多需要 6 bit 有效精度。非零乘积绝对值位于 `2^-10` 至 `2688`，
都在 FP16/BF16 正常数范围内；两种格式分别具有 11/8 bit 精度。
所以在本次有限输入域中，转换和中间乘法精确，只有最后转为 E4M3
时发生舍入，不会引入双重舍入误差。

CPU 生成器逐个检查全部 4,064 种标量组合的中间乘积能被两种格式
精确表示，包含符号零。初始参考用 binary64 精确乘积与有限 E4M3
输出枚举；完成 GPU 后，又用独立的 `2^-10` 定点整数参考与二分查找
核对两条组合的完整输出，均通过。

NaN scale `0x7f/0xff` 未纳入，本次不声称 NaN 行为等价；正的有限
NVFP4 scale 域已完整覆盖，另外也覆盖负 scale、±0、subnormal、
nearest-even tie 与正负饱和。

## 冻结矩阵与实机结果

8 个独立进程，各执行 1 次原生 kernel；每个 kernel 4,129 组四元素
向量，33 blocks × 128 threads，尾部线程有边界保护。输出前后各有
16 字节 canary，每进程申请设备输入/输出共 66,096 字节。

向量构成为：1 组冒烟，16 种 FP4 × 254 种有限有符号 FP8 scale
在每个 lane 的覆盖，另加 64 组混合 lane 输入。每 kernel 核对
16,516 个标量结果；这是标量组合的四 lane 覆盖，并非枚举所有
packed 向量的笛卡尔积。

| kernel | 功能 | 四元素向量逐位通过 |
|---|---|---:|
| Decode_e2m1_f16 | FP4→FP16 | 4,129/4,129 |
| Decode_e4m3_f16 | FP8→FP16 | 4,129/4,129 |
| Scaled_f16 | FP4×FP8→FP8，经 FP16 | 4,129/4,129 |
| Decode_e2m1_bf16 | FP4→BF16 | 4,129/4,129 |
| Decode_e4m3_bf16 | FP8→BF16 | 4,129/4,129 |
| Scaled_bf16 | FP4×FP8→FP8，经 BF16 | 4,129/4,129 |
| Roundtrip_e2m1_f16 | FP4→FP16→FP4 | 4,129/4,129 |
| Roundtrip_e4m3_f16 | FP8→FP16→FP8 | 4,129/4,129 |

合计 33,032 组 packed 输出、132,128 个标量结果，0 mismatch，
全部 canary 保持。加载、launch、同步、回读及 context 销毁全部成功，
8 个进程退出码均为 0，未发生超时。只运行 `sm_110a` 原生 cubin，
SM100a 仅参与 CPU 汇编与字节比较，没有将其模块加载到 Thor。

设备 NVIDIA Thor、compute capability 11.0，driver API 13020，
加载后的 binary version 为 110。PTX JIT 与 cache 只在实验子进程
禁用，模块使用 eager loading；没有补丁或未公开指令参与本次执行。

冻结 plan SHA-256：
`3c4e98db2e3a10db143d2d0a0ded3c9a61815ea53527bc1283cf3c98e4f1d7a8`。
Thor cubin SHA-256：
`67892ced9508c106a04c6fa8a332cfeb93b99ccb1f0a8783338616380ab9d2da`。

## 隔离、清理与证据

全部工作位于 `.q4t-work/cuda134-qmul4-20261103/native-convert/`，
复用原隔离 CUDA 13.4.92 工具。host 使用现有 g++-14、CUDA 13.3
头文件和系统 libcuda，`-Wall -Wextra -Werror` 零警告。

完整阶段由项目 `gpu_exclusive.py` 持有同一跨会话锁：实际驱动
12.252 秒，lease 13.189 秒，14 次观察无竞争或未知进程。终态
COMPLETED，锁释放；锁内末次 NVML 计算上下文为空。轮询不能排除
短于采样间隔的干扰。限定运行窗口的内核日志无新 Xid。

环境变量、系统 nvcc/ptxas 路径/版本/哈希、CUDA 链接、libcuda 实体
路径/哈希的前后快照完全一致；默认仍 13.3.33。没有加载模型、改动
生产源码/默认程序、安装软件包、重置 GPU 或更换驱动。

[完整原始证据](evidence/cuda134-qmul4-20261103/native-convert-records.json)
封存 67 份文本、17 份完整数值缓冲区、3 个本机 ELF 身份，共
424,999 字节。数值缓冲区以 zlib+base64 无损保存，包含输入、所有
CPU 期望、所有 GPU 实际输出及 canary；解压后逐字节匹配本机原件。
编译器原始失败、全部 PTX/SASS、host/生成器/驱动源码、独占日志、
环境与参考来源均保留。47 项离线核对通过，没有重跑 GPU 找有利结果。

封存 JSON SHA-256：
`725203560c46778f1ed3d69d9a416d0d40816e955796137af9cf047b23ce84c4`。
先前编译、离线解码、QMUL4 非法指令封存均不修改。

本轮证明的是转换与缩放组件可用；没有验证完整 W4A8 GEMM、模型质量
或吞吐，也没有证明存在不同编码的单条 QMUL4。若用于推理引擎，应
另按实际运行时和性能任务要求验收。
