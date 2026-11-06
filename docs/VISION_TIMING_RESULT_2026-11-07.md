# 视觉分段计时与nvJPEG优先级结论

2026-11-07。原始来源6fc6c6f2，独立诊断程序SHA256
`1d217e7d15a44d3fa5b9f4667d84a3043a138b136ed2fa7fa00a63ca96bb6162`，
协议来源`99b7e4b6…`。按[冻结计划](VISION_TIMING_PLAN_2026-11-07.md)
完成有限诊断；不接受性能优化，不替换默认程序。
[PR #18](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/18)两项host
检查通过，main合并12311a30的树等于7b9776e5。重复阶段分支已清理，
[保留锚点](evidence/vision-timing-20261107/mainline.json)记录旧tip与主线。

## 结论与下一步

在视觉流水线内部，当前固定输入的主耗时段是forward，占该流水线墙钟的
**98.33%～99.63%**。JPEG单图解码**2.754～2.782ms**，占其整次HTTP
E2E的**0.0775%～0.0792%**；多图中的JPEG解码2.820ms，占该请求E2E
0.0236%。在只消除JPEG解码、其他执行保持不变的理想假设下，当前
单图也仅能节省约2.8ms。这不是nvJPEG实测收益，也不外推大JPEG批量业务。

本轮决定**暂缓nvJPEG接入**。后续若继续性能工作，先限定到视觉塔内部
的attention、投影/MLP GEMM及位置表处理分段，再选具体实现。当前
forward仍包含CPU提交、位置表构造/上传及GPU完成等待，不能据此声称
某个kernel已被证明是瓶颈。此次不继续展开该项计时或追加采样。

## 已交付计时合同

`Q4T_VISION_TIMING=1`显式启用，默认关闭；stderr每个视觉请求一条
`[q4t][vision-timing]`。13段分别为prepare、decode_png、decode_jpeg、
resize、normalize、patchify、pack、pixels_allocate、pixels_upload、
workspace、output_allocate、forward和cleanup。保留各段调用数、
items/frames/patches、total_ms、other_ms与completed状态。

不改HTTP或模型计算，不新增CUDA同步/event。CPU阶段使用steady_clock；
forward利用原有cudaDeviceSynchronize边界。workspace含原有释放和表
准备，上传是原cudaMemcpy调用墙钟；它们不是纯CUDA设备计时。resize
含原尺寸复制及视频尾帧补齐，视频decode含帧所有权复制，cleanup调用数
含空指针释放尝试。总时间含本地退出清理，不含前置HTTP/base64解析、
preflight、model锁等待或后续LLM执行。日志本身的stdio输出在总时间
采样之后；默认关闭不读取阶段时钟或输出诊断行。

分项非嵌套，残余other_ms不隐藏；解析器拒绝缺失/重复、非有限/负值、
错误codec次数、身份不符或不闭合记录。completed=0不作为完整测量。

## 固定HTTP观察

同一候选先关闭再开启统计，greedy、单槽、no-MTP、max_len=16384，
同一租约2模型、18Chat、22控制、0原生GPU worker，**158.894秒**。
前8题两模式成对，开启组再重复PNG舱内和JPEG各1题。无自动重试。
下表单位毫秒，forward为上述组合边界；首请求与后续请求单独列出。

| 开启组输入 | PNG解码 | JPEG解码 | 视觉总计 | forward | forward占比 | HTTP E2E |
|---|---:|---:|---:|---:|---:|---:|
| JPEG地球，进程首请求 | 0 | 2.782 | 1363.659 | 1352.587 | 99.19% | 3589.168 |
| PNG舱内 | 12.705 | 0 | 8329.166 | 8298.043 | 99.63% | 11421.586 |
| 同像素PNG地球 | 6.303 | 0 | 1305.371 | 1291.228 | 98.92% | 3496.676 |
| 舱内PNG＋JPEG地球 | 12.790 | 2.820 | 9623.545 | 9583.278 | 99.58% | 11951.455 |
| 真实记录图表PNG | 0.385 | 0 | 1600.188 | 1588.938 | 99.30% | 3396.784 |
| 已有8帧视频PNG | 2.778 | 0 | 759.614 | 746.959 | 98.33% | 1997.044 |
| JPEG地球，重复1 | 0 | 2.754 | 1302.812 | 1291.816 | 99.16% | 3483.471 |
| PNG舱内，重复 | 12.634 | 0 | 8323.923 | 8292.491 | 99.62% | 11400.444 |
| JPEG地球，重复2 | 0 | 2.764 | 1303.467 | 1291.733 | 99.10% | 3490.570 |

PNG地球由本项目stb解码原JPEG后无损保存；CPU预处理全缓冲逐字节相同，
没有缩放、重压JPEG或替换问题。首/后请求位置仍不同，不能把该表两行
E2E之差归因图片格式。CPU预处理加BF16打包总计8.89～37.21ms；
像素上传约0.106～0.135ms，workspace准备约0.95～1.91ms。

全部18题答案、JSON/SSE终态、preflight/usage通过。前8题开关两组
message、finish_reason、usage完全一致；纯文本不产出视觉计时。
成对E2E原值如下，保留小幅正负观察，不宣称统计非劣或零开销。

| 输入 | 关闭，秒 | 开启，秒 | 开启相对变化 |
|---|---:|---:|---:|
| JPEG地球，首请求 | 3.595860 | 3.589168 | -0.186% |
| PNG舱内 | 11.415111 | 11.421586 | +0.057% |
| 同像素PNG地球 | 3.498085 | 3.496676 | -0.040% |
| 多图 | 11.953739 | 11.951455 | -0.019% |
| 记录图表 | 3.383986 | 3.396784 | +0.378% |
| 已有视频帧 | 2.001332 | 1.997044 | -0.214% |
| 文本保护 | 0.819836 | 0.818304 | -0.187% |
| JPEG地球，重复1 | 3.489446 | 3.483471 | -0.171% |

## 原始失败、修复与证据有效性

原驱动在第二个模型退出时复用Children台账。reap清空children而保留
started，导致第二轮的真实僵尸PID与第一轮登记PID比较失败。18次HTTP
和9条完整计时均已完成，但原阶段为**FAILED**、GPU guard为
**COMMAND_FAILED**，第二轮reaped/custody回执缺失；这些原件不改写。

修复将每个模型生命周期使用独立Children台账，整阶段共享GPU租约不变。
两个真实CPU子进程序列用例通过：旧复用方式复现错误，新台账两轮退出
通过。GPU确认仅在该CPU测试中模拟，因此不声称修复后重新通过GPU门禁。
未再次加载模型或发送HTTP。

独立离线核对2581文件绑定、2302Git来源，逐条从HTTP原件重建答案、
usage和计时，而非只读驱动PASS字段。两模型均已有身份匹配的waitable/
exit=0记录；第一轮完整custody/reaped存在。第二轮退出之后原guard
记录了一次NVML空上下文，完整监控无竞争/不可读项；退出后的只读检查
确认两PID不存在、18146无监听且可复用绑定、GPU为空、原inode3294067
锁可取得并释放。最初未设SO_REUSEADDR的端口绑定检查返回地址占用，
与TIME_WAIT相符；随后确认无监听，按实际服务复用方式成功绑定。
未终止其他进程。

因此结论单列为**SCOPED_TIMING_ACCEPTED_WITH_DRIVER_FAILURE**：可用
于本次瓶颈诊断，原完整阶段退出门禁失败保留。轮询仍不能排除短于
采样周期的干扰。本轮没有优化或生产部署接受。

CPU另有333host（含4项计时合同）、6项日志解析负控、8组图片/视频
预处理开关逐字节对照及2项取消/额度部分记录检查，零警告构建。
模型计算、CUDA kernel、原同步和资源顺序未改；既有HF全塔合同只在
其未变范围复用，不扩大模型质量或视频/并发接受。

## 使用、回退与保留边界

诊断程序位于`.q4t-work/vision-timing-20261107/engine/q4t`；在已获授权的
GPU独占阶段驱动中，将该程序作为serve入口并设置`Q4T_VISION_TIMING=1`。
完整示例参数与固定请求见`tools/vision_service/timing_stage.py`。取消该
环境变量即可关闭统计。默认`build/q4t`仍为ea1b474d，sidecar仍为
3eb46385，旧托管/任务/Key/day包和所有原依赖保留。

原件及失败现场在`.q4t-work/vision-timing-20261107/`；内容寻址副本与
originals.json保存测试时身份，后续驱动修复和只读核查单独绑定。
[机器结果](evidence/vision-timing-20261107/result.json)与
[绑定清单](evidence/vision-timing-20261107/bindings.json)公开全部观察。
无常驻服务、自动恢复或额外GPU请求；nvJPEG、视频硬解码、视觉并发和
长期验证均未扩展。
