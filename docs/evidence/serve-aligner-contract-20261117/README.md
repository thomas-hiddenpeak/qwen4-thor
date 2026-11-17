# 原生对齐资产合同

固定Qwen/Qwen3-ForcedAligner-0.6B revision
`c7cbfc2048c462b0d63a45797104fc9db3ad62b7`：资产下载/哈希通过，
1,840,070,940字节、640.541秒；结束剩余9,011,716,096字节，
保持8GiB预留。没有CUDA初始化、模型加载或HTTP请求。

8项针对性CPU通过，0.015613秒：真实配置、结构漂移、预处理、
分类元数据、独立5000类head、tensor范围/类型/重复、原ASR互斥
及真实checkpoint只读头部。708个BF16张量，共1,835,457,792字节
数据；权重完整SHA见资产回执。原ASR正例及混用反例在同组核对。

首个构建未重新configure，找不到新增target，原日志保留；随后
configure/build成功，`-Wall -Wextra -Werror`，不计成测试失败。

仅证明固定资产/配置合同。tokenizer、实际计算、字幕接口、真实
业务、恢复和交付仍须完成；默认rev276没有替换。
