# 原生 Realtime 文本与工具对话可回退交付 rev356

默认引擎 `f5af36c6c5d7232f9b180be8c9100616d0cfbe73b30f77135cb4b91a12eb609d`，服务包
`c292e8049b1cba76e3596faa7b7b0c2b135abe258458abfafded3434107fc1fd`。19 项 STOPPED 发布、实际整根回退与再次
前进通过，314.086 秒、20 次命令尝试，0 新增 GPU；服务保持
STOPPED，outer 启用且累计启动 0。前驱 rev346 及恢复入口保留。

本次交付 `/v1/realtime` 原生文本与函数工具对话：共享正常模型准入，
支持真实工具回填、流式终态、逐轮 RPM/TPM、取消及实际 token 结算。
[主轻资格](../serve-realtime-chat-primary-light-20261119/README.md)和
[保留/TLS/整包恢复](../serve-realtime-chat-managed-20261120/README.md)
覆盖实际资料工具和已配置的转录、语义 VAD、ICL/语音等能力保持。
恢复三组 4 模型、63 HTTP+21 Realtime/15 WebSocket/1 CUDA 探测，
647.374 秒、604 次正常 NVML，自有资源释放。

原首次发布因 intent 128/128 容量拒绝，默认和 journal 均未切换；
8 条已退休请求通过现有维护工具原子迁出，0 删除，可按原计划恢复。
沿同一请求与原验收元数据继续完成，原 FAIL 不改写。维护另用
7.073 秒。已完成的 CPU 缓存及 speaker 数值中间结果
保留 SHA 和无损恢复入口；未删除模型或合格程序，原路径复核须先恢复。

alpha RPM30/beta RPM1、两端 TPM6000、alpha 输出3457及已用额度保持；
私有验收夹具 TPM60000 未覆盖持久策略。profile 历史/冷却、版本周期
与 outer 预算保持。候选→保留实际自动整包恢复通过；反向完成登记
及 STOPPED 根回退，未另注入反向自动故障。

原 TLS 指令不遵循仍是模型质量 FAIL，按用户确认与恢复协议分别记录；
默认采样参数不改。原整包 NVML ENV_ABORT、路径复核、诊断及数值/
声纹失败均保留，用户延期听感仍待验。后续成功不能追认原错误。

PR #52 最终提交检查及实际合入另记。Realtime Prompt、音频、MCP、
追踪、Live 与其余 master 持续实施；此交付不表示完整 OpenAI 兼容、
全模型质量、性能或长稳通过。
