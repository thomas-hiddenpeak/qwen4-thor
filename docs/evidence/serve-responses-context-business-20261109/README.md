# Responses 自动上下文裁剪真实验收

2026-11-09：生产候选 bb6e702e 通过。累计 2 次模型启动、7 条请求、
93.414 秒（result.seconds 已包含首轮，不重复相加），91 次 NVML 全 OK。
首轮真实 JSON 和分词结果复用；首驱动失败及修复在相邻 driver-fix
证据目录完整保留，未改生产候选、未重跑已通过请求。

真实旧前缀超过 8192 容量，JSON/SSE/后台均只保留当前问题及
instructions，42 输入 token 与实际 preflight 一致，答案 42；
disabled 与单当前任务过长均真实返回 context_length_exceeded。
长历史下实际文档函数调用、previous_response_id 与结果回填、严格
JSON 标题/引用均通过；完整原 input_items 保留。清理确认、端口释放。

本组非性能业务，只针对改动；未做全性能/全模型或视觉 GPU 复测。
默认 N06 尚未替换，下一步合格发行与可回退发布，然后继续 A07.2
及总计划全部余项。
