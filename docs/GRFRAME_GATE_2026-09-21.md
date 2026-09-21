# GRFrame gate 独立工作区候选

日期：2026-09-21。基线 e550a02，正式性能参考仍为 fcb5925。
当前状态：已接受；完整 HTTP、数值、布局与 4K 时间线均通过。

## 实现与约束

主层 gate[T,hc] 在 decoder workspace 尾部独立分配逻辑区域；容量与
forward 仍共用 MakeDecoderWorkspaceLayout。先前各区域的 offset 保持，
总预算追加 AlignUp(T*hc*2)。8192 分块预计增加 65536 字节，T=1 因
256 字节对齐增加 256 字节；不是整机峰值增加的实测值。

attention Read→子层→Write 与 MLP Read→MoE→Write 按 caller stream
依次复用同一 gate 区域。gate 必须跨子层有效，不能与 normed/down/up
或 MoE/attention/GEMM 暂存重叠，辅助 stream 的原有 join 继续保留。

GatedResidualFrame 可接受 GatedResidualGateStorage；有 gate 时先检查
空指针和容量，再执行 mix。借用 gate 不由 frame 释放，缺省调用仍自有
分配，Release 清空所有权状态。借用存储须存活到已排队 Write 完成，
若放弃帧也须覆盖未完成的 Read；host 析构不表示设备工作已完成。
use_combine=false 与 tokens<=0 不需要 gate，保留既有语义。

本轮没有改动数学 kernel、舍入顺序或主层之外调用方式。接口增加了
所有权分支，不能将更少 cudaMallocAsync 直接等同于净复杂度下降。
实际时间线确认每步少 48*2=96 对异步分配/释放，362→266。

## 验收与证据

证据：`.q4t-work/e2e/grframe-gate-arena-20260921/`。
候选 SHA-256：`4d3cbae5433e3fc188f16abc478d2951e9c30aec90f7459ab8aea7f8b802fcda`。

1. 构建后第一项为 tools/evalscope HTTP 质量，再用同一二进制跑完整
   五档，每档同输入三次、输出 256，MTP 关闭。
2. 完整结果分别与直接父版及固定参考比较 TTFT/decode 重复范围，
   有不利分离则停止，先做 HTTP 复核。
3. 只有上述门禁通过后，才编译执行数值及布局工具，再跑 4K 时间线。
4. 数值工具保留 fcb5925 原始 HC 数学作 oracle；除中间值/输出逐位
   对照外，还覆盖所有权切换、放弃帧、拒绝后重用、gate 保护区。
5. 布局工具以 e550a02 为对照，检查新增 gate 区域及旧偏移，并链接
   实际候选静态库核对公开容量。工具执行前核对二进制/源码/库指纹。

门禁后数值与布局工具已执行通过，构建零警告；时间线已完成。[机读合同](../dataflow-engine/plans/grframe_gate.json) 目前不是执行器输入。

质量结果：11/11，HTTP/输出检查通过，failure=null，服务退出 0。
同一二进制五档性能 15/15 通过，输出摘要一致、服务退出 0；
父版及固定参考均无不利范围分离。正式性能参考不重置。

## 完整 HTTP 与门禁后检查

| 输入 token | TTFT 均值秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.829283 | 18.582365 |
| 4096 | 2.789437 | 17.911915 |
| 8192 | 5.489546 | 18.149139 |
| 45056 | 31.781153 | 17.890385 |
| 204800 | 164.871737 | 17.103747 |

TTFT 包含 HTTP、分词、prefill，不是纯 prefill。样本均值变化很小，
性能按持平理解，不宣称稳定提速。

30 组、4953108480 个有限 BF16 值逐位一致；所有权切换、gate 两端
保护区、放弃帧、错误后重用及 no-combine 检查通过。252 布局、
504 次实际链接容量核对通过。模型 workspace 在 8192 分块下
2347958272→2348023808 字节，增加 65536 字节；不是整机峰值测量。

## 时间线与接受决定

4K HTTP 的 prefill 异步分配/释放各 362→266；255 个 decode forward
各 92310→67830，即每步少 96 对。prefill kernel 87703、decode kernel
442935，逐 kernel 调用数完全相同，计数 D2H 仍为 prefill=48/decode=0。

profile 窗口 prefill 3007.320→3018.383 ms，decode
14438.551→14412.884 ms。采集受 profiler 扰动，不用于正式性能结论。
完整五档支持按持平保留此阶段，正式参考仍为 fcb5925。

主层 normed/down/up/gate 均由 decoder workspace 提供，Read/Write
仍执行原数学路径。兼容路径继续拥有分配，接口的所有权分支是明确代价，
不是净代码量减少。下一步应把已验证布局与 GR 执行依赖绑定成可检查的
资源描述，先保留现有内存位置及执行顺序，再考虑其他子层迁移；此前
linear 暂存合并曾因 TTFT 回退撤回，不直接重复该方案。
