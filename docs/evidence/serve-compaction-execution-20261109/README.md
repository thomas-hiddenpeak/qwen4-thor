# A07.4 压缩执行与协议针对性 CPU

2026-11-09。23 项 C++、5 项 Python schema、2 项官方 SDK 检查最终全通过；实际验证 57 个 JSON/SSE 值，host/引擎零警告。0 GPU，不等于真实模型压缩验收或默认交付。

实现真实压缩模型步骤的执行入口、自动阈值与显式 trigger、独立 compact 原生路由、共享输出/截止/取消/累计 TPM 与单次 RPM、认证回填及原始历史保留。C++ 验证使用明示模型文本/token 数夹具；实际引擎 `0c577f54` 已构建，尚未执行本组 GPU。

首次 22 C++ 通过 21，4096 项边界落入通用500而非400已修；仅失败边界和变更事件补验。schema 暴露既有 SSE 填充错误：原先所有事件都有 obfuscation，已按固定 SDK 的 include_obfuscation 文档改为 delta-only。SDK 类型对多数 delta 漏列该可选字符串，schema 明确记录此适配，其他事件继续拒绝额外字段。新增填充测试的序号夹具曾清空前缀，已修夹具并仅补该1项；未改生产逻辑。全部原日志保留。

独立 compact 保留用户消息，返回可直接回填的窗口；SDK 把它声明为 OutputItem[] 与其保留用户消息要求不一致，仅 compact 返回 schema 适配为输入窗口。实际官方 SDK 已消费 C++ compact JSON 和自动压缩 SSE；新反例检查保证非增量事件及错误类型填充仍被拒绝。未把结构形状通过当作所有 executor 已实现。

源码、schema 准备期间 inventory 字典处理与直接执行工具的 Python 导入路径错误已修，记录见 schema-preparation.json。私有测试密钥/对象目录未纳入证据。复用未变证据；具体执行数与二进制身份见 decision.json。

下一步：冻结真实模型/SDK 的事实保留、文档工具闭环、实际 token 缩减、后台回放、配额/清理短验；通过后可回退交付，再继续 A07.5 及全部主计划。默认 A07.3 dca3b450/STOPPED 保持。
