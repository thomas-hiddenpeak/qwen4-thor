# messages 入口模板 HTTP 诊断（2026-09-23）

## 结论

当前服务没有把真实messages请求转换成模型原生聊天模板。
24个有效HTTP响应、服务退出0，六组配对均显示messages与旧
`role: content`拼接的输入长度、完整输出和结束原因一致，而非原生模板。
该入口一致性检查失败；采集退出0不代表正确性或性能验收通过。

| 输入形式 | 严格答案正确 | 原生模板长度一致 |
|---|---:|---:|
| 原生prompt，前次 | 6/6 | 6/6 |
| messages（显式enable_thinking=false） | 0/6 | 0/6 |
| 旧拼接prompt | 0/6 | 0/6 |
| 原生prompt，后次 | 6/6 | 6/6 |

0/6表示本次输出合同失败，不是六道算术题全部算错：前五组messages
进入`<think>`文本并耗尽32token，未返回要求的最终答案；空白案例
立即stop且内容为空。不能据此推断增加输出预算后永远答不对。
关键问题是显式关闭思考未作用于输入模板，以及角色/生成边界缺失。

## 冻结设计及原始核对

六案例：system-user、user-only、多轮更新、text parts、中文、
首尾空白。每例依次native-before→messages→legacy→native-after。
独立六位答案在推理前确定；所有请求greedy、stream、max_tokens=32，
MTP关、max_seq=1、max_prefill=8192、max_len=208896。
原生prompt从只读模型chat_template.jinja经Transformers 5.17.0渲染，
enable_thinking=false；真实messages携带相同chat_template_kwargs。
模板SHA256：`c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041`。

逐条核对evalscope SQLite内实际发送字段，确认messages没有被客户端
改写为prompt；核对SSE全文、usage和finish_reason。前后原生六组
重复一致、输入长度均同tokenizer；messages六组均同旧拼接长度。

| 案例 | 原生输入token | messages输入token | messages输出/结束 |
|---|---:|---:|---|
| system-user | 52 | 40 | 32 / length |
| user-only | 34 | 24 | 32 / length |
| multi-turn | 90 | 71 | 32 / length |
| text-parts | 52 | 40 | 32 / length |
| chinese | 46 | 36 | 32 / length |
| whitespace | 51 | 43 | 1 / stop，空内容 |

原生各请求均7输出token、stop、六位答案正确。长度和输出相等本身
不证明内部prompt字节完全相等，但与实际源码的旧拼接路径一致。

证据：`.q4t-work/e2e/chat-template-entrypoint-20260923/`下的
`responses-audit.json`、`paired-review.json`、`driver-exit.json`、
原始`client/`数据库、请求、服务日志与命令。运行时为恢复二进制
2a5813f2，源码没有修改。未涉及数值测试、kernel profile或旧bench。

准备脚本此前把性能恢复通过作为启动条件；原脚本/计划另存
`run_http.original.py`、`plan.original.json`。本次明确改为要求有界
恢复正确性确认及性能复核完成，因为这是未修改运行时上的HTTP
诊断，不是通过性能门禁后才允许的低层分析。启动时恢复状态完整
复制进证据，performance_validation_closed仍false，不抹除疑点。

## 修复范围与验证顺序

代码位置：src/server/chat_server.cpp的messages遍历与RenderContent。
应以模型模板为合同，形成独立于GPU执行的请求渲染职责，保持裸prompt
路径供原生输入对照。不能仅修补六道题或追加assistant前缀。

需要覆盖的合同包括：

1. im_start/im_end、换行及trim；system只允许首条，空消息与非法角色错误。
2. enable_thinking默认值、reasoning_effort的xhigh/medium/low、
   generation prompt，以及显式false时的空think结束块。
3. assistant历史中的reasoning_content、preserve_thinking与最后用户轮次。
4. tools定义、历史tool_calls参数和连续tool结果的包装；明确协议适配，
   不静默丢弃字段。输入工具模板支持不等于输出工具调用解析已实现。
5. text parts与视觉占位边界、system视觉拒绝；现有video_frames是项目
   输入扩展，需要与模型video占位合同明确衔接，不能假设天然相同。
6. 原生prompt路径和视觉资源/占位顺序保持可核对，不改变模型计算精度。

先冻结上述分支的HTTP配对及错误响应案例，再实现单一模板改动，
必要构建后的第一项仍为tools/evalscope HTTP E2E。重复本次24请求，
并跑固定质量、五档性能及受影响视觉/工具/多轮案例；失败保留。
只有E2E门禁满足后才做内部模板字节或数值微测试，不能以格式器
单测代替实际入口。性能未闭合部分须显式报告，不自动视为接受。

本轮仅完成入口问题定位与证据冻结；尚未修复，也不证明默认思考、
工具、视觉或全面多轮行为已经验证。下一步按完整合同实现与验证。
