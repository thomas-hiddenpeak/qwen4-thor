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

## 候选实现与首轮验证（未接受）

候选二进制SHA256：
`5e6d2ee6bf83d6aba18822c4e712b19ffca64501c56d1428c17ccd5b3777d44d`。
新增server/chat_template职责，HTTP内容只解析一次；裸prompt保持原文。
实现默认/显式思考选项、Unicode trim、历史reasoning保留、工具定义与
历史调用/结果包装。OpenAI工具arguments字符串先解析为模型所需映射；
JSON保留数字原文避免大整数经double舍入，原数值访问器不变。
视觉资源仍按内容顺序收集，补vision_start/end及可选编号，非法内容报错。
尚未证明全部分支符合合同，不包含新的输出工具调用解析器。

必要构建0且零警告后，首项测试是原24条evalscope HTTP：messages
六组均与原生长度、全文、finish一致，6/6正确；原生前后各6/6，
旧拼接仍0/6，服务退出0。证据chat-template-fix-20260923。

扩展15组×3=45条HTTP已完成，覆盖默认/显式思考、low/medium、
空system、Unicode空白、历史保留/丢弃、空assistant、工具定义、
映射/JSON字符串工具参数、连续工具结果、当前轮思考保留及
工具响应形式user消息。大整数9007199254740993、小数/指数、
负零、Unicode/换行嵌套参数均包含在冻结参考中。15组原生前后
重复、messages长度/全文/finish全部一致。仅5组预置独立答案，
其15条全部正确；其余10组只判定差分一致，不宣称最终答案正确。
证据chat-template-fix-extended-20260923，服务/采集均0。

固定质量门禁首次脚本错误使用quality根目录而非quality/inputs，
启动服务前失败，证据chat-template-fix-gates-20260923保留。
修正脚本后v2会话75569运行固定11题→完整五档15请求；不是重试
模型失败。当前未完成，不做低层测试、不接受候选或重置参考。

后续已冻结但未运行：18种错误输入HTTP；六种视觉对照（单图、
双图、视频，分别开关编号），四服务每种三次共72请求。视觉对照
让旧恢复二进制接收历史手工原生模板/空角色，让候选接收正常
user消息，复用相同媒体字节；验证格式迁移，不要求新接口容忍
旧空角色。固定质量/五档、视觉、错误输入尚待终态和原始核对。


## 五档与错误输入终态（候选仍未接受）

chat-template-fix-gates-v2-20260923已终止，质量11/11、五档15条
原始请求/输出同正式，服务均0，源码摘要与二进制5e6d2ee6绑定。
五档均值TTFT为0.795665/2.633614/5.175149/30.499485/159.495128秒；
decode为18.563104/17.857014/18.083501/17.855480/17.091813 tok/s。
全三次范围对正式/固定/恢复各0项不利；后两次子集分别3/2/6项
不利，raw-gates-audit.json保留逐项范围。不得用一个口径覆盖另一个，
不将HTTP全部完成当作性能已接受，不据此启动低层分析。

chat-template-fix-invalid-20260923已完成20条：18种非法请求均
返回预期HTTP400与对应错误，前后正常messages请求均200、答案
720041且stop，服务退出0。通过evalscope的process_request结果
旁路导出状态及错误正文，再与原始数据库请求逐条匹配；未修改
已安装工具包，不把evalscope失败计数当成意外模型失败。

视觉四服务72条已启动，证据chat-template-fix-vision-20260923；
独立15题回归准备完成但未启动。源码和构建保持冻结，未接受候选。


## 视觉输出一致，但时延门禁出现不利项

chat-template-fix-vision-20260923共72条完成、四服务退出0。
六种输入各12条（参考前/候选/候选重启/参考后，各三条）的
input/output token、完整文本与finish全部一致且非空。这证明
已测视觉输入的模板迁移保持行为，不是独立视觉理解正确率。

latency-comparison.json另存完整三次范围的48项比较，其中8项
不利：候选重启的双图带编号decode、视频不带编号decode均低于
前后参考；视频带编号decode在候选及候选重启两组均低于前后参考。
不能因文本一致而忽略时延失败，因此候选仍不接受。不临时扩大
容忍比例，也不重复采样寻求通过。独立15题回归已启动，会话71505，
先收齐当前冻结版本的质量记录，保留原有4K错误，不修改源码。

下一项可隔离的修正方向是缩小工具数字原文保留的影响范围：
当前额外字段扩大了全项目每个Json对象，且所有模型配置数字都
保存原文，而精确保留只由聊天工具序列化需要。可考虑仅在HTTP
请求/工具参数解析时启用，并复用数字类型未使用的字符串存储。
这是源码可见的额外开销，并未证明是实测decode差异的根因；
任何替代实现都要重新构建并从HTTP E2E开始，不能凭推测接受。


## 收齐首版独立答案，应用按需数字原文候选

chat-template-fix-answers-20260923已完成：15条全文同正式，
14/15正确、无新增失败，4K仍600440而非360284。质量评估器1、
服务0、外层证据采集0，不改答案或参考。首版5e6d2ee6保留于
prepared/chat-template-fix-20260923/q4t-candidate及source/。

待旧服务全部退出后应用一个范围调整：Json移除新增成员，普通
模型/配置解析默认不保留数字原文；HTTP请求及工具arguments解析
显式开启，在数字类型未使用的str中保存原文，数值访问器不变。
这是消除不必要全局开销，未证明首版回退由此引起。

新二进制2392f6b1fb33f28744d0e63812e47be98b4c6f224e22f087bd627d050f8bbe9e，
构建0且零警告，首项为原24条evalscope HTTP，会话8853。新证据
统一chat-template-scoped-json-20260923前缀。后续仍需扩展、固定
质量、五档、错误输入、视觉与独立答案；不接受候选，不运行低层
测试，旧失败证据和所有冻结输入保留。

首项24条已完成：messages6/6且六组长度/全文/finish均同原生，
前后原生各6/6、旧拼接0/6，服务0。后续会话92721串行运行扩展、
固定质量/五档与原始审计、错误输入、视觉及独立答案；无自动
性能接受，全部结果仍待核对。当前候选保持2392f6b1冻结。
