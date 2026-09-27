# Decode计算失败的请求终态修复

2026-09-27：有界故障及HTTP回归完成。默认build/q4t仍为b5f38e48，
未部署本候选，未进行性能接受。

## 缺陷和修复合同

[完善度审计](PROJECT_READINESS_REVIEW_2026-09-27.md)实际注入单序列
4字节decode D2H错误：已部署版本对流式/非流式均返回正常stop，
输出截断为`7`，success=1、error=0，同时健康已503。这与先前
prefill读回错误的500处理不同，必须独立修复。

本次保留调度decode、直接decode及MTP调度步的失败标记。离开调度
列表并完成原有序列清理后，先按现有协议仲裁取消，再确定响应终态。
尚未被取消的计算失败请求：

- 非流式返回HTTP 500、`server_error`、`generation_failed`。
- 流式头已经发出，HTTP状态仍200；发送明确error事件和[DONE]，
  不发送正常finish_reason或成功usage。客户端必须检查SSE错误。
- 不增加成功计数，错误计数加一；指标HELP改为包含生成阶段失败。
- 原有GPU不健康标记、完成同步与槽位释放不改变。后续工作仍按
  GPU健康状态拒绝；不是自动恢复故障GPU。

同时修正非流式tokenizer Decode失败发送500后仍计成功的问题。
此分支仅完成代码修正，尚未单独注入tokenizer故障。

取消先赢得仲裁时仍走取消终态；本节点没有新增请求状态枚举或改变
取消凭据/期限合同。`RequestControl::Finish`在这里代表终态发布权，
不代表推理计算成功，成功统计由响应路径决定。

## 身份与验证

候选SHA256：
`f410c3c6c424d28b4a0267cee6efb7dff80f9c1c947be8f53aa0100d2f61c99a`。
构建来自`.q4t-work/sequence-slot-build-20260927/`，实际CMakeCache、
源码快照和构建日志保存在`.q4t-work/decode-error-candidate-20260927/`。
必要构建零警告；格式换行后重构建二进制SHA相同。

| 检查 | 状态与范围 |
|---|---|
| decode copy/sync × 流式/非流式 | 4/4通过，max_seq=1，MTP关闭，合成返回码注入 |
| 每个decode故障后的指标/健康/槽位 | success=0/error=1/aborted=0；健康503；空闲槽位1/1；后续生成503 |
| 四个故障服务停机 | 均退出0 |
| prefill copy/sync | 2/2通过：HTTP500、槽位2/2归还、健康及后续生成503，服务退出0 |
| 请求取消矩阵 | 8组通过：排队/prefill/decode、显式/期限/FIN/RST、凭据复用、存活请求及停机；服务退出0 |
| 固定11题evalscope HTTP | 11/11通过，全文同当前参考，含200K；原始11条SSE/usage/stop复核，服务退出0 |
| 五档性能 | 本节点未运行，不声明性能无回退或更新正式参考 |

新增可复现工具：
[run_decode_failure.py](../tools/evalscope/run_decode_failure.py)。
`--fixture-plan`读取原readback测试的命令及请求，`--binary`显式指定
候选；输出目录必须新建。该工具不提供性能或整模型数值结论。

首轮工具局部函数名遮蔽http模块，在发送请求前失败，服务退出0；
原目录`.q4t-work/e2e/decode-error-fault-20260927/`保留。修正工具后
四组通过的证据在`.q4t-work/e2e/decode-error-fault-v2-20260927/`。

本轮直接故障注入覆盖正常调度的单序列decode；没有证明MTP、直接
回退路径、多活动序列同时故障、GPU硬挂起的完整行为。代码修复覆盖
部分共同终态，不把单流证据扩大为这些分支已经实测。

## 后续范围

140个文件在所有验证服务和驱动退出后完成摘要封存，入口为候选目录
`artifact-binding.json`；SHA256：
`ececc32fbd9b6f55a832befe78870956bb81a5bf1801525311a85a75013803f6`。
封存脚本首次误用退出记录字段server_exit（实际为server），未生成manifest；
修正后复核原始记录再封存，没有改变测试结果。

JSON深度/数字/字符串、HTTP framing、
字段范围、媒体解压预算和测试门禁仍是独立待办，不因本修复提交而
消失。默认部署保留原身份，待后续输入治理和完整接受矩阵后再明确
升级；不存在未测试的性能接受结论。
