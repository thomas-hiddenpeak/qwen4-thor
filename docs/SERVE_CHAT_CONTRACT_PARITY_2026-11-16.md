# Chat 样式与流式协议补齐

2026-11-16，A01/A02/A12 的实施子项，全部 Serve 总任务仍 active。
本项是兼容性与业务行为修复，不是性能优化；候选尚未验收或发行，
实际默认仍为 rev150/a7bd6785。

## 必须实现的合同

- `stream_options` 对象只用于 `stream=true`，支持布尔
  `include_obfuscation`，缺省为 true。角色、正文、推理、工具参数及
  choice 终态都经过同一实际传输实现；usage-only、DONE、错误和心跳
  保持原语义。padding 不计模型 token，显式 false 关闭。
- `reasoning_effort` 接受 none/minimal/low/medium/high/xhigh/max 和 null。
  缺省/null 改为标准 medium；显式 xhigh 保留原模型提示。嵌套选项
  存在时优先，包括 null。none 使用模型原生闭合 thinking 前缀；显式
  enable_thinking=false 仍关闭推理。其他级别使用真实提示适配，不能
  宣称固定计算量或质量保证。旧 xhigh 缺省模板证据保留原身份。
- `verbosity` 接受 low/medium/high/null；缺省/null 为 medium。low/high
  实际进入模型指令，保持原消息和来源坐标。与 Responses 复用既有
  指令文本，Responses 此组不改变默认策略。
- 生成器、公开请求/响应 schema、capabilities 和实际客户端同步。
  Python 客户端需保留 usage 要求，同时允许显式混淆开关；标准 JS SDK
  与原始 wire 的重组结果一致。不能仅通过 allowlist 宣称支持。

官方依据为 2026-11-16 抓取的
[Chat 创建协议](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
冻结参考的其余七项顶层字段、历史 audio/annotations、媒体状态缓存及
其他 33 项总任务仍须实现，不随本组删项。

## 针对性验证与交付

冻结记录位于 `.q4t-work/serve-chat-contract-20261116/`：
`freeze-01.json` 与 `freeze-cpu-01.json`。15 项 C++ 覆盖直接输入、
错误路径、原生模板、Unicode 来源、概率/工具 SSE、能力和共享 Responses；
2 项 schema、1 项 Python 和1项 JS SDK 直接客户端检查；来源 guard
及生成物一致性检查。失败保留，只复验受影响项。

必要构建使用独立 host/engine 目录，保护已发布默认程序，要求零警告。
CPU 通过后同一严格 GPU 租约最多 1 次模型启动、6 条 Chat、900 秒：
实际 JSON/SSE、混淆开关、样式提示与答案、真实文档工具执行/续答、
多 choice usage、同 PID 普通文本及自有资源清理。不追加完整性能或
全模型质量矩阵。业务通过后完成适用资格迁移、可回退发行和主线提交；
本组完成后继续其余 master，实现完成和验收完成分别记录。

## 当前结果

[针对性CPU证据](evidence/serve-chat-style-cpu-20261116/README.md)通过；
15项不同C++，6项schema/客户端/驱动，32项输入对照。首轮夹具失败和
定点修正保留；业务与发行仍待完成。其他未改行为不重复全矩阵。
