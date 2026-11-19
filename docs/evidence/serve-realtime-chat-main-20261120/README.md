# 原生 Realtime 文本／工具服务合入主线

[PR #52](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/52) 已于 2026-11-19T17:51:43Z 合并。
两组公开host CI均在实际提交 `74759c6540d4440066820b5bf1e4c685aac7d9f3` 上通过；
main合并点 `0c8d7a1076574010d9753f556fa22ae745cb68fc`。完整检查/合并回执见归档。

包含已验文本与工具会话、主轻资格、Guardian恢复组合及rev356
可回退发行。原TLS回答质量FAIL、NVML环境中断、数值/声纹失败与
延期听感仍保留，不将模型质量错误记为正确。默认程序仍为rev356，
本次仅合入与来源核验，0新增GPU。

后续Prompt注册表、设置组件与会话接线未进入该PR。当前工作分支
有独有提交和未提交开发，继续保留，不删除其证据路径。Prompt、
音频/MCP/追踪/Live及其余master任务继续，总目标仍active。
