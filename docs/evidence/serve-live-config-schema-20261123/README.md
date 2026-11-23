# 将实际配置响应绑定到发行 schema

发布前使用严格 JSON Schema 验证真实 `/v1/config`，发现顶层有七个已返回而未声明的字段，嵌套的 `application_startup.tracing` 也未声明。修复覆盖媒体程序身份、SIP 状态、两种 TTS 模型和注册声音的健康状态、合成限制以及追踪诊断。追踪快照不可读时，用 null 表示未知；已记录的累积计数仍为整数。历史生产者可省略新增字段。

两种引擎身份在主、轻配置各一份原始 HTTP 响应，同时通过独立 schema 和发行 OpenAPI 内嵌 schema 校验，共八次。协议回归检查通过 8 项，涵盖退化状态和非法类型。验证仅使用保留的响应。[原始响应摘要](http-schemas.json) · [协议检查](contracts.txt)
