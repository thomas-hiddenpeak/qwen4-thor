# messages 工具客户端开发

后续有界业务验收已通过，见[结果与证据](CHAT_BUSINESS_ACCEPTANCE_2026-11-02.md)。
候选830694ce未替换默认程序，GPU已释放；以下保留此前开发快照。

后续更新：当前代码已增加能力查询门、持久日志与只读恢复判定；
客户端合同增至22项，全部未执行。当前demo需要提供capabilities接口
的新候选；本页以下19项与354b20ae为初版开发记录，最新接入/验证
命令以[聊天接入准备](CHAT_INTEGRATION_PREP_2026-11-02.md)为准。

## 范围与状态

2026-11-02 用户要求 vLLM 工作期间安排其他开发、不测试。本阶段选择
标准 messages agent 接入，基于396a401的待验收输出协议；不改推理引擎、
聊天模板、部署程序或业务服务。只进行源码开发及Python语法编译，
不执行CPU测试、模拟HTTP、真实HTTP、模型加载或自动CI。

冻结范围：标准库客户端统一读取非流式与SSE；完整重组正文、推理、
按index排列的工具调用；只有finish、usage、DONE及EOF完整才交出响应。
工具参数在应用执行前全部解码并交给显式注册的校验器；完整assistant
消息与tool_call_id结果回填到下一轮历史。提供有界只读lookup示例与
离线合同用例，待设备有可用窗口后统一验收。

不增加强制工具选择、约束解码、通用JSON Schema验证、工具自动重试、
多agent调度或SDK全兼容声明。既有raw缓存示例和冻结验收入口不改写。

当前状态：实现与19项离线合同用例已准备，仅Python语法编译完成，
**所有运行检查未执行**。编译不等于协议或业务通过。服务端候选仍为
本阶段的354b20ae；后续[工具历史关联](CHAT_TOOL_HISTORY_2026-11-02.md)
候选b4f04236包含该输出协议。两者均待验收，默认0c5ab1de/sidecar
e6fdd13b不变。

## 接口与职责

[chat_client.py](../tools/examples/chat_client.py)只用Python标准库，要求
Python 3.11或更新版本。面向本项目的单choice、greedy、解析messages
协议；不是通用OpenAI SDK，也不直接适配当前vLLM业务端点。

| 接口 | 行为 |
|---|---|
| `make_request(messages, tools, ...)` | 深拷贝完整历史和工具定义；每次生成新的request_id/cancel_token；可显式复用session_id |
| `ChatClient.capabilities(...)` | 显式GET并检查能力/模型，可固定协议与模板摘要；失败不自动降级或生成 |
| `ChatClient(base_url).complete(body)` | 一次HTTP POST，支持origin或`/v1`地址；返回统一Completion；不自动重试或跟随重定向 |
| `read_sse` / `read_json` | 可独立消费响应；保留content、reasoning_content、JSON字符串arguments及usage |
| `prepare_tool_calls(completion, validators)` | 按名称白名单解码和校验整个调用批次；返回执行计划，不执行工具 |
| `append_tool_results(history, completion, results)` | 要求每个call id恰有一个字符串结果，按调用顺序追加assistant和tool消息 |
| `append_final(history, completion)` | 仅stop且没有工具调用时追加最终回答；length保留在响应里但不自动提交 |
| `ChatClient.cancel(body)` | 显式使用原请求凭据发送取消；202仅表示接受取消请求 |

工具定义仍由应用提供。输出JSON解析拒绝重复键、NaN/Infinity、数组型
arguments及无效UTF-8；JSON大整数使用Python整数，arguments原文不重写。
工具参数的业务校验由注册的纯validator负责：成功返回None，失败抛
异常，布尔返回值不当作通过。不把服务器的类型转换当作
完整schema保证。客户端不执行模型生成的shell、模块名或网络地址。

SSE支持CRLF/LF、注释、多个data行，以及按index交错到来的函数名称/
arguments片段。工具id须在首个片段给出，后续不能改变；同一响应id、
model和choice index必须一致。Q4T要求独立usage尾帧；任何错误、重复
终态、缺失DONE、DONE之后的数据或EOF前断开均失败。不提供提前执行
工具的增量回调；`sink`只用于保存原始响应字节。

响应默认最多32MiB，单个SSE事件8MiB，最多128次工具调用；超过即失败。
请求最多16MiB。HTTP socket超时比本次服务端排队/生成截止多10秒，
它是单次阻塞IO超时，不宣称严格的整轮墙钟截止。应用应保持业务请求
和工具执行预算；没有自动重试、断点续跑或工具副作用的事务保证。

历史辅助函数返回新列表，完整保留assistant reasoning_content和
function.arguments原文，不把推理重复拼入content。模板重新渲染仍可能
改变分词前缀；复用session_id不是缓存命中保证，按usage观察实际结果。

## 只读工具往返示例（当前不要运行）

[agent_tools_demo.py](../tools/examples/agent_tools_demo.py)演示应用如何
显式校验并执行本地只读lookup，再把`{"code":"831907"}`作为工具结果
提交给模型。默认最多4次生成请求、8次工具调用；正常任务为工具请求
加最终回答两轮。每轮默认512输出tokens，可用`--max-tokens`选择
1～512（统一验收固定256），120秒服务端截止。没有后续请求
预算时不执行本轮工具，length/解析失败/HTTP错误均停止。

工具注册、参数校验与读取RECORDS在示例里清晰分开；批次内任一未知
函数或非法参数，会在整个执行计划交出前拒绝。业务工具需要由应用
实现授权、幂等与副作用恢复；本例只有进程内字典读取。

以下命令会真实发送模型请求，只能在输出协议候选完成必要验收、设备
业务空闲且阶段持有共享GPU锁时执行；当前仅作为后续接入说明保存：

```bash
python3 -B tools/examples/agent_tools_demo.py \
  --base-url http://127.0.0.1:18096/v1 \
  --directory .q4t-work/agent-tool-demo-01
```

地址必填，不默认指向当前8000业务端口；脚本不启动、停止或替换服务。
用`--thinking`保留思考往返，`--no-stream`选非流式。服务显式启用私有
会话缓存时可加`--session-cache`，脚本生成一次session_id并跨轮复用；
没有启用服务缓存时不要加此参数。模型名可用`--model`显式指定。

输出目录必须是`.q4t-work`内的新目录，已有目录拒绝重跑。目录权限
显式0700、文件0600；每轮请求包含取消凭据，应按私有会话材料保存。
在字节限额内保留生成响应原始字节、完整响应对象、调用计划、每个
工具开始/结果记录及完整历史；HTTP错误保留最多64KiB错误正文。
仅最终任务完成写completed.json。失败保留failed.json及已有现场，
不自动重新执行已经开始的请求或工具。历史文件不是自动恢复入口。

## 待执行验证与边界

[test_chat_client.py](../tools/examples/test_chat_client.py)提供19项离线
合同定义，接入公共host CTest的`q4t_agent_tool_client_contracts`。
覆盖实际报文字段的SSE/JSON一致性、交错分片、UTF-8、结束顺序、EOF
异常、限额、参数与大整数、历史不变、请求凭据、显式取消、只读工具
往返及失败不执行工具；HTTP部分使用内存响应/替身，禁止真实网络。

当前没有执行该脚本、CTest、模拟HTTP、客户端示例或真实模型请求；
也没有重新配置CMake，以免触发其中的进程能力探测。只对三个Python
文件调用`compile(source, filename, "exec")`，不导入或执行其内容。
构建/源码身份记录见[开发证据](evidence/agent-tool-client-20261102/README.md)。

后续先执行离线合同，再安排真实客户端工具往返；与服务端协议验收
共同协调业务空闲窗口、共享GPU锁和有界预算。原服务端21生成/10负例/
3启动计划保留，本轮不自动扩大或执行该计划。两项开发快照均未部署，
未宣称工具成功率、模型质量、缓存收益或性能验收通过。
