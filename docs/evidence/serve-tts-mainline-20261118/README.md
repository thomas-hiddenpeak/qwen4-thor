# 原生语音输入输出与重叠处理已合入主线

[PR #48](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/48)已实际合并，
main为`81bf3d1e35dfbf9566957be35f3a3c3980b1bcac`。最终工作提交
`7675c6625d61891f5fdb52454ffb602d32baddae`的push与PR两组检查均
SUCCESS；合并树逐Git tree核对完全相同，新增26条语音与活动取消
合同也在公共CI通过。原先本机真实业务证据复用，0新增GPU。

默认rev316的引擎、服务包、发布元数据和核心恢复账本摘要保持；
仍为已交付STOPPED状态。4倍内容失败原型没有进入本次PR，主线
能力不能被解释为完整Speech或全部OpenAI兼容通过。其余voice、
Realtime及全部master任务继续，当前工作分支因后续独有工作保留。
[合并前检查](before.json)、[实际合并回执](after.json)、[身份核对](result.json)。
