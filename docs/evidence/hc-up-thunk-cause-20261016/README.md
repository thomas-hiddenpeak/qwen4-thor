# 地址不变的跳板干预实验

本目录先冻结协议、派生脚本和全部身份。诊断副本只在.q4t-work，不能
部署；A0/B0是原始SHA相同的复制，A1/B1分别仅将6/5处跳板分支恢复为
相同LDR，所有地址、文件长度和非.text段逐字节不变。派生脚本验证
每个入口的唯一B目标、LDR编码及跳回地址，不重新链接或调整padding。

实验目的、固定8条顺序与停止条件见protocol.json；同身份两次是
负控/漂移界限，第二组反转顺序。不是五档验收，不自动接受性能。
详见[专题报告](../../HC_UP_TTFT_CAUSE_2026-10-16.md)。


## 执行结果

- observations.json、review.json：完整8条与冻结判据计算，两组均未满足。
- completion-audit.json、lease-exit.json：8个HTTP退出/合同及全阶段监控。
- raw-manifest.json：本机保留的遥测、监控、服务日志与结果的SHA256。
- environment.json：诊断环境；遥测缺少三个/proc/pressure文件，不能证明
  无系统压力；监控只覆盖轮询可见进程。
- symbol-duplicate-audit.json：旧按名称比较的范围限制。
- hc-registration-disassembly.txt：HC独立启动注册的代码证据。

目标仍active、原NO_GO保持。没有部署二进制或接受新性能身份。
