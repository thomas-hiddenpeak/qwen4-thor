# P0 CPU证据

`result.json`记录413项CPU合同、生产编译和未运行范围。
`source-bindings.json`记录变更源码、编译产物、协议身份及默认/视觉
程序保护校验；ELF留在本地，不纳入Git。

`raw-records.tar.gz`无损保留所有本阶段命令日志和结果，包括三处
入口/生成校验失败、初始非最终补丁、host与runtime内嵌协议来源、
编译命令。`manifest.json`列出每个成员的长度及SHA256。

公开host测试与最后preflight拒绝入口不在同一协议身份，差异明记；
该适配层不属于公开host编译目标，生产程序增量构建通过。这里没有
真实模型HTTP、CUDA数值或运行验收，不证明视觉候选或生产交付通过。
