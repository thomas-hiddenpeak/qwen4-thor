# 原生 Decisions 请求与答案合同

同一固定版本的官方 SDK 生成请求与结果的类型图，重新生成逐字节一致。原生请求检查服务模型、题数和选项、工具结果归属、共享输入缓存边界，以及 JSON 和图像输入。Live 增删条目复用相同的工具字段规则。答案投影保留原始类型与顺序，布尔概率按两个候选归一化，有序分值为等级位置的加权平均；每道题可单独拒答。用量来自执行器，缓存与推理细项按各自归属汇总。

14 项针对性 CPU 检查通过，实际引擎零警告构建。该组测试注入受控分值验证数值和协议，模型取分与 HTTP/Live 执行仍需真实验收。

[CPU 日志](cpu.log) · [构建与类型图身份](result.json) · [官方接口参考](https://developers.openai.com/api/reference/resources/decisions/methods/create) · [官方评分说明](https://developers.openai.com/api/docs/guides/decisions)
