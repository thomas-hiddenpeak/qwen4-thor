# 视觉行最大值优化：受影响 MTP 补验

2026-11-07 用户要求继续验收；完成后再讨论服务降级与自恢复。
本轮不开展新的厂商根因诊断，不扩展服务能力、硬解码、并发或长稳。

## 身份与复用

沿用候选34e7ceef、父982bba07及c6eef3c2运行时；不重编译推理程序。
原先导64条视觉/文本HTTP、26组逐位数值、四份HF、普通decode五档与
22质量题按封存摘要复用。核对原Git来源、CAS原件、完整模型退出与
custody及退出后的成功NVML观察；这些生命周期均在原失联之前结束。
原完整阶段仍为ENV_ABORT，原HOLD决定和缺失MTP custody不改写。
不把旧阶段中断的MTP 1K/4K或后续诊断九条算入本次矩阵。

## 本轮固定预算

一个严格GPU租约，顺序为MTP候选、MTP父，每身份一次启动。
五档1024/4096/8192/45056/204800各3请求、256输出，随后11固定质量题；
合计最多2模型、52Chat、16控制、0原生GPU用例、3600秒。沿用原输入
字节、容量208896、prefill8192、单流、greedy、默认k3完整head；不启
媒体或诊断。环境清理及原始evalscope采集代码复用，严格NVML不降级。
其中最后45秒留作清理；不自动重试或扩样，失败保留并只停自有进程。

启动入口为`python3 -B -m tools.evalscope.resume_vision_rowmax prepare`，
再由`tools/evalscope/gpu_exclusive.py --output
.q4t-work/vision-attention-rowmax-20261107/mtp-completion-01/lease --
/usr/bin/python3 -B -m tools.evalscope.resume_vision_rowmax run`持有整段租约。
准备时绑定新工具commit、两个ELF、输入、依赖及共享锁原inode。

## 决定与交付

维持[原验收判据](VISION_ROWMAX_ACCEPTANCE_2026-11-07.md)，逐档公开
TTFT、decode、HTTP全部负点；视觉投入门与文本保护项分别审查。
原视觉结构化SSE发送尾部不称模型decode。测量重复层级、统计不足和
可归因退化否决不变，不事后引入容差，不因微小差值追加请求。

完整补验与独立原件审计后，形成新的合并证据与工程决定；新决定引用
原ENV_ABORT而不覆盖原决定。若接受，另一次严格租约最多1模型4Chat、
16控制、300秒验证JSON/SSE及真实只读工具闭环。保留原默认pair回退，
同ELF健康通过后同步默认程序与发布元数据；严格外层租约完成才最终
确认交付。旧包不迁移，全局media_acceptance仍false。未通过则保持
原默认，不把监控缺口或仅部分矩阵记作性能通过。
