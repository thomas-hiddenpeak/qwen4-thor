# 栈采集修复及子集部分观测证据

结果见 [阶段报告](../../SESSION_PWRITE_STACK_RESULT_2026-10-29.md)。
完整独立归档：`/home/rm01/models/dev/qwen4-thor/.q4t-work/session-pwrite-stack-20261029-archive`。
archive-manifest.json 绑定所有原始文件；archive-check.json 为独立副本
逐项 SHA 校验。保留 149,900,416 字节 events.bin、栈表、计数、全部
CPU/HTTP/监控失败和实际 frozen-tools，不因大小丢弃有效证据。

current-tools 为事后管理队列过滤修复，只有 CPU/构建证据；不可与实际
HTTP 源码混同。旧完整 head 记录保持原归档和原 ENV_ABORT 全局状态，
仅复用已通过的 10 HTTP/2恢复及闭合文件窗口。新阶段仍是 ENV_ABORT，
不是完整子集观测通过，不改变默认或旧推理发布决定。
