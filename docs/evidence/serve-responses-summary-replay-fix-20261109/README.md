A07.2 实际摘要与回放缺陷修复

首轮真实三种摘要已生成，JSON/SSE/后台和 usage、实际客户端账本通过；回放已有 SSE 终态后因缺少 HTTP 零块被官方 SDK 拒绝。真实首轮保留 FAILED，1 启动/3 请求/49.951 秒，正常清理。

原因是 ServeResponsesState 写完 replay 后才置 kTerminal，传输 FinishResponse 未见 terminal write，不能补零块。改为完整成功写完后 kComplete，5 项直接 CPU 通过、必要引擎/host 构建零警告。摘要模型与数值执行源码未改，复用已过生成；实际续验仅回放持久旧记录与余下 5 请求，累计最多 2 启动/8 外部请求/13 内部生成/900 秒。原对象目录不改。
