# SRV-06 文本停止合同与有界验收

起点b5ba57f0，当前已接受默认31ffc41e/9a71a0a7不随开发替换。
这是业务协议补齐，不是性能优化；长期工业化目标继续active。

## 冻结合同

- `stop`支持单个字符串或1..4个字符串；每条1..256 UTF-8字节，精确
  匹配、大小写敏感、不做Unicode规范化。缺省/null/[]禁用（[]保留原兼容）。
  形状/空串/过长错误在模型工作前400，param精确到数组项。
- 匹配对象是生成文本，先按现有Utf8Stream修复非法UTF-8，再进入响应
  解析器；包含推理与工具标记，但不匹配prompt。最早完成者优先，同一
  完成位置按请求列表顺序；与token/网络块边界无关。
- JSON/SSE共用增量匹配；只保留可能成为停止串的尾部，最多255字节。
  命中串和以后字节不发布。EOS/length时释放未命中前缀；首个完整匹配
  优先于同token的长度界，finish为stop（已有完整工具按tool_calls合同）。
- completion usage计至触发匹配的实际token，包含停止串所占token；停止
  可能位于token内部，不能把可见文本重新分词当usage。MTP完整批已提交
  的KV保持原状态再销毁，逻辑停止后的投机后缀不计completion、不发布，
  不声称没有额外GPU计算。自然EOS的原计量保持。
- 普通parsed messages在推理内命中可返回未闭合推理的stop；这是用户
  主动停止，不能伪造答案。普通auto工具仍须完整解析；截断调用返回
  response_parse_failed，无成功finish/usage，客户端不得执行部分结果。
  strict/required/named/parallel=false、JSON约束与有效stop组合首批拒绝；
  state cache组合亦拒绝，避免跨过逻辑停止的MTP状态被发布为响应快照。
  禁用stop的中性值不引入上述拒绝。raw prompt/parse_response=false保持raw。
- 推理预算与stop可组合：预算决定实际选择token，stop只决定何时终止
  返回；hidden reasoning仍参与匹配和计量，不是正文隐私过滤。

参考：2026-11-03读取的[OpenAI Chat接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
规定最多4个停止串并从文本中排除；特殊组合、256字节限制、usage和
推理终止是本项目明确profile，并非官方普适承诺。只读vLLM源码
67111973的v1/engine/detokenizer.py提供缓冲与最早完成/列表次序参考；
本项目按实际逻辑消费token计量，不宣称所有投机后缀计量与vLLM相同。

## 验收冻结

先成组实现再统一运行：host ON/OFF合同（含输入、独立oracle与任意
块切分、UTF-8、重叠、部分工具/推理）、生产native适用合同与实际后端、
正式客户端输入/终态、schema及清单漂移门。原模板/模型/数值不改，
按身份复用已通过证据。不得用helper通过替代生成循环的实际停止。

真实HTTP在同一GPU锁内完成MTP、plain、安装健康3个启动，最多4次尝试；
最多22次生成相关请求、16个专门输入负例，首次execute起1800秒，
含失败且不重置截止。冻结计划21次生成：无stop基线及同usage上限的EOS竞争、JSON/SSE英文
跨token、中文、重叠、未命中前缀、长度竞争、raw、thinking/hidden、
budget组合、普通工具截断错误、实际只读工具回填、plain及安装健康。
请求不使用外部付费模型，监听loopback；失败保留原件，仅重验受影响项。

退出要求：正确前缀和终态、JSON/SSE一致、实际MTP批与停止trace、
不泄漏尾部、SDK消费、资源归零/无残留进程、guard完成。通过后按既有
pair/seal/回退链交付并有界安装健康；小幅时延观察不作性能非劣声明。
计划的细化用例与驱动需在第一次HTTP之前封存，不能在结果后改预期。

当前：CPU开发检查通过。host OFF188/ON197各有1个夹具错误，原失败保留；
原生生成前缀修正后，仅重验该项，两种构建均通过。生产154必需合同、
9个真实grammar、客户端28、驱动8通过；实际HTTP与新默认验收待执行。
一次新增测试range-loop转换警告已修正，最终生产构建零警告。
