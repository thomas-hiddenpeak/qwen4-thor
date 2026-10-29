# pwrite 修复阶段证据

结论见 [结果](../../SESSION_PWRITE_REPAIR_RESULT_2026-10-29.md)。
完整原始归档（含 229 MB HTTP events.bin、三次启动、失败及冻结源码）：
`/home/rm01/models/dev/qwen4-thor/.q4t-work/session-pwrite-repair-20261029-archive`。

archive-manifest.json 逐项绑定全部文件；archive-check.json 记录独立副本
逐项 SHA 校验。版本库仅保存小型摘要、门禁与测试日志，原始记录未按大小
丢弃。http-stage/frozen-tools 为实际 HTTP 版本，current-tools 为事后
CPU 修复版本；两者不能混为同一受测身份。完整归档保留 ENV_ABORT，
局部有效窗口不改变整个阶段失败状态。默认程序/发布元数据不变。
