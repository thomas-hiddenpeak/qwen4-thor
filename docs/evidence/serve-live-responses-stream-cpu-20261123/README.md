# 原生 Responses 事件通道

8项新用例与6项直接受影响的HTTP用例全部通过。实际 ResponsesOutput 产生文本、函数调用、用量与终态，测试通过脱离 socket 的 HTTP 执行上下文接收这些事件。覆盖有限错误、损坏或跨响应的序列、背压、超时、取消和执行器排空。服务端 Live 委派的真实 SDK 验收待完成。

[运行日志](cpu.log) · [运行清单](required.txt) · [来源与构建身份](result.json)
