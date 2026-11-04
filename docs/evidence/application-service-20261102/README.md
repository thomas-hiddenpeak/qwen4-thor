# 应用服务首批原始证据

归档 275 份原始文件，1,942,677 字节；封条
`sealed-files.json` SHA256：
`c0733ba21995e222b51dc5c8ad4161dd753ac4dd6cb16c104d7d770aa2a6af0a`。
本说明不在原始文件封条内。

- initial：首次 CPU / SDK HTTP 已通过项、历史查询预算失败与原封包。
- correction：预算修正后真实业务和安装健康通过，末尾 NVML import
  失败与自动回退；原 guard COMMAND_FAILED 保留。
- final：修复监控解释器后的无新增模型请求收尾、COMPLETED guard、
  已安装 pair 身份与最终空闲检查。

子目录中的原封条对应原完整本机包，可能包含未复制到 Git 的二进制；
本归档自身的 sealed-files.json 只覆盖实际复制的原始文件。不要混用
两者。7 份保留二进制的路径 / 大小 / SHA 见 retained-binaries.json。
wire.bin 为原始 HTTP 正文 / SSE，保留原换行，不能格式化或裁剪。
构建候选、SDK 环境、模型权重均未纳入 Git。

结论、失败分类、身份、复用范围与回退命令见
[本次结果](../../APPLICATION_SERVICE_RESULT_2026-11-02.md)。
