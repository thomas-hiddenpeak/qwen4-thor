# 固定 k3 suffix：报告路径绑定缺陷修复

本阶段只修上轮已证明的本地过强条件：同步范围已生成报告、实际目标
APIStop0 已返回，驱动仍要求 Generated stdout 及时出现才绑定路径，
导致45秒超时。旧 ENV_ABORT 与失败原件保留。推理程序、head、suffix
准入、Nsight 版本和 CLI 选项均不变。

## 具体合同

保留 `repeat:1:sync` 的应用线程报告生成屏障及同一实际目标 PID/
start_ticks/input_tokens/成功返回收据。独有规范输出目录启动前不得有
报告；屏障后只绑定唯一匹配冻结 trace 前缀的普通非空报告，拒绝多份、
异目录、符号链接及不稳定 inode/大小/mtime，封存并复核 SHA。
Generated 不及时出现不再阻塞；若出现完整路径且与报告矛盾仍拒绝。
不猜固定范围编号，不把稳定文件本身当完成或 trace 完整性证明。

实际目标独立退出、所有权、共享 lease、监控、known-work/负控、实际
目标和未知 warning、成功 memory API 活动覆盖门全部保持。原始 NVLOG
继续留存，无法清门时保留不完整结论，不把文件生成当全部 GPU 忙闲已解释。

## 事前预算

`.q4t-work/suffix-report-binding-20261026/scope.json` 冻结7200秒工作墙钟。
受影响 CPU 为 runner19 + synchronous（最多16），总至多35方法/180秒；
一次 suite，仅具体失败修复允许一次修复 suite，仍共用总180秒。复用
身份未变的 manual16/automatic14、86项 monitor/fixture/analyzer、native64
及原有推理正确性和两 head 重复收益，不为文档或代码包装重跑模型。

唯一完整持锁阶段上限4800秒，direct/wrapper各一次共600秒（各300秒）；
只有控制通过才依次8K off/on、200K on/off，最多4模型加载、6 HTTP。
200K前置8K、输入及256输出、缓存和请求顺序继承同一原计划。零构建、
零运行时改动、零重试/CLI选项/版本扫描。任一竞争、监控或自有清理失败
记ENV_ABORT；预算或完整性门失败停止依赖分析，不自动扩样。

## 身份与范围

源入口 a7b24756，当前默认程序 f356b2c9、sidecar 422993b0、子集manifest
b63b0f25；观察程序 f0ff0fe8 / 源55363f18，min32对照6a4d3d62。精确完整
身份由冻结计划、CPU源快照、父证据和独立审计绑定，不把简称当校验值。
完整head重复+13.6774%/+14.4034%、子集共享B描述性GM+6.5335%/+6.7304%
按未变身份复用，不抵销原五档保护NO_GO。当前仍非suffix交付接受。

并行只读源追踪确认线性注意力 a/b 的 GEMM workspace 实际为32MiB：
decoder_layer 的 kGemmWs 经 layout.attention_bytes 原样传入，再由
linear_attention 原样传给 Bf16Gemm。配置N48/K2560、verify M4已有明确
来源；这只补齐未来固定参数probe的工作区，未执行probe，不倒推旧6381
次memset count或是否缺事件，prefill实际M与实际algo仍需分别绑定。

## 结果

32项CPU（runner19/synchronous13）首suite全通过，0.525162秒，零失败/
错误/跳过；407源前后SHA不变、临时目录无残留。已通过静态独立审阅。

唯一持锁阶段16.055184秒，direct/wrapper两控制均完成：APIStop0、唯一
报告绑定、正常shutdown、目标退出和launcher回收；没有强制清理或监控
失败，17/17采样，lease已释放。绑定各耗时0.000836/0.000893秒，绑定时
Generated均未出现，实际验证了修复路径。此项限定合同通过。

两组各32 kernel、64 copy、32非零memset（16×512B/16×16384B）、33 NVTX
完整，删除kernel对和最后NVTX的实际副本负控均拒绝。实际目标仍各有
CUDA/NVTX完整性warning，完整性门拒绝，0模型加载/0 HTTP，不追加采集。
这是正常有界STOPPED，外层COMMAND_FAILED源于门禁，不是监控ENV_ABORT。
CUDA警告与forced flush有支持性线索；指定事件完整不能证明任意模型完整。
两组均存在producer NVTXFinish；旧离线review对此窄匹配漏项的文字由
[补充核对](evidence/mtp-suffix-report-binding-20261026/nvlog-reconciliation.json)
明确纠正，consumer警告原因仍未知。

独立完整审计一次16.232049秒：46696冻结文件、21297父快照、82 raw及
197模型元数据一致，环境/所有权检查无错误；但审计FAIL/INVALID原样
保留。唯一失败是direct NVLOG早期收据的整文件SHA/长度与最终文件不同。
其916678字节是最终922220字节的精确前缀；追加5542字节29行均为nsys
daemon3422064收尾，目标3422087的514行及全部终止/loss字段相同。追加
日志中session-storage ENOENT保留，不推导daemon无异常。一次60秒上限
局部核对实际0.060889秒验证前缀、最终raw seal与目标行；没有重跑完整
审计或将其翻为PASS。wrapper收据与最终文件一致。

| 决策字段 | 结论 |
|---|---|
| 证据有效性 | 全审计INVALID；具体缺陷限于早期NVLOG整文件收据，局部前缀核对通过；两组已知工作与报告绑定证据可按其范围引用 |
| 测量完整性 | 两控制完成；0/6 HTTP、0/4模型，200K/8K GPU对照未完成 |
| 统计结论 | 诊断阶段不评估吞吐、TTFT或统计非劣 |
| 工程决定 | NOT_ACCEPTED_INCOMPLETE_TOOL_QUALIFICATION；默认及发布元数据保持，原运行时保护NO_GO保持 |

[冻结、CPU、首次审计和法证原件](evidence/mtp-suffix-report-binding-20261026/README.md)
按身份保留。完整目标仍active；下一步仅允许基于实际失败的NVLOG快照
口径修复，以及用已绑定生产GEMM形状核对memset真实count的有界独立
参数探针。后者与Nsight不能同时CUPTI订阅，不反推旧6381调用，不扫描
形状/版本。工具诊断不能代替适用五档HTTP、质量及各性能保护。

568条目/56584339字节独立副本及SHA回读完成，seal `d5683aaab387`。
原FAIL/INVALID、局部核对、CPU源快照及raw保留；非自包含，继续依赖
四层父归档和原路径。归档后的本条收据不在自身payload内，另由阶段
提交保存。未执行部署或健康HTTP。
