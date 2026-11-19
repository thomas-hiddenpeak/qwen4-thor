# Realtime Chat 业务驱动 CPU 与原生构建

2026-11-19：10 项 CPU 反例检查 / 0.378 秒通过，0 GPU。
5 项 JavaScript 检查覆盖单一终止、真实用量、完整且白名单工具调用、
文本与参数流一致；5 项 Python 检查覆盖冻结范围、真实项目来源、
重复终止、取消实测、带外隔离、实际 RPM 拒绝及结算。

原生候选构建 345.370 秒，零警告，SHA256
`c9dc2c669be46855b15ef3003dded47017d84815a4313be131dc6abb35c1c1ca`。
构建源码 `72c4c4c06`；后续仅增加验收驱动与文档。默认 rev346 STOPPED
及保留程序身份未变。原件索引见 `manifest.json`，公开结果见
`cpu-result.json` 和 `build-result.json`，私有源码/日志完整保留在
`.q4t-work/serve-realtime-conversation-20261119/`。

下一步真实短验上限：1 模型、1 TLS 入口、3 WSS 会话、8 次响应、
1 次转写、2 次普通 Chat、900 秒，同一严格 GPU 租约。使用固定官方
OpenAI SDK 7.47.0 / ws 8.21.0，读取实际项目发布记录闭环，不预写工具
结果；检查取消、输出限额、RPM/实际 TPM 结算及共享转写回归。
PID 绑定入口控制器为验收夹具，不能冒充 Guardian 恢复资格。

本证据仅为 CPU 和构建通过，尚未验收实际模型业务；不宣称性能、
完整模型质量或整个 Serve 目标完成。原量化/声纹失败与延期听感保留。
