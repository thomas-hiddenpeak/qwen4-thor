# 官方 SDK 的流式兼容记录

### 较早 Python SDK 读取程序化工具

Python 2.8.1 的严格 `responses.retrieve` 不能解析较晚加入的 `program` 项和 `programmatic_tool_calling` 工具。其通用 `client.get(path, cast_to=dict[str, Any])` 可读取两种原生资源。订阅时使用 `stream=True`、`stream_cls=openai.Stream[dict[str, Any]]`、`options={"params": {"stream": "true"}, "headers": {"Accept": "text/event-stream"}}`，续读将 `starting_after` 加入 `params`。2.8.1 和 3.55.1 均完整读取两份真实记录的 144 和 6 个事件以及两个后缀，程序代码、工具定义和调用者字段逐项等于原件。3.55.1 的高层严格入口也通过。[程序化工具读取记录](evidence/serve-mcp-error-sdk-20261127/program-sdk-read.json)

### Responses 的 MCP 错误表示

原生 TLS 下，Python 2.8.1 严格客户端批准读取项目文档，并用另一次审批拒绝同一工具。实际只读取文档一次。旧客户端完整收下拒绝结果的 38 个事件，3.55.1 读回原响应、错误对象和相同事件，两侧均从第 6 号游标续读 31 个事件。四次跨客户读取均返回 404。模型启动一次，用时 53.7 秒；下一步验证配置和托管恢复。[业务短验](evidence/serve-mcp-error-sdk-20261127/business.json)

稳定版 Python 1.80.0 至 3.0.x 将 `mcp_call.error` 定义为字符串。读取 Responses 及其 SSE 时，q4t 将整个错误对象编码为 JSON 文本，保留异常类型、内容和嵌套字段。Python 3.1.0 起和没有协商版本的客户端读取对象。成功调用的空错误保持原值。持久资源和事件均保留同一份结构化错误。[上游类型变更](https://github.com/openai/openai-python/commit/f38355ecdf69b231be218e572f591ce7f389a211)

旧版 Python SDK 2.8.1 严格模式已可读取实际被拒绝的 MCP 调用，并完整回放 38 个事件。错误正文中的代码和消息可逐字段还原。3.42.1、3.55.1 仍收到结构化错误，三个版本也都能读取成功调用的 54 个事件。原生新建事件与落盘记录保持对象形式。程序化工具的旧版客户端兼容和真实 TLS 发行验收仍待推进。[CPU 验证](evidence/serve-mcp-error-sdk-20261127/cpu.json)

### Responses 的缓存驻留字段

原生 TLS 网关用一次模型启动完成严格客户端验收，用时 41.4 秒。旧版客户端读到了原先报错的历史响应。新旧客户端分别查询发行记录，各自回答 558；互换客户端后，GET 正文和 SSE 事件仅缓存驻留字段的拼写不同。旧版和新版异步客户端还在正文中段关闭连接，分别从同一游标取回 18 个后续事件。七次跨客户读取均返回 404。默认服务保持停止。下一步验证发行包交接。[业务验收](evidence/serve-response-cache-retention-20261127/business.json)

稳定版 Python SDK 从 2.8.0 到 2.32.x 使用 `in-memory`，2.33.0 起使用 `in_memory`。原生入口按 SDK 自带的语言和版本头选择 JSON 和 SSE 响应的拼写，`Vary` 声明两项请求头。两种输入写法对应同一缓存策略，`24h` 可跨版本使用。没有版本信息的客户端收到当前拼写。持久记录和事件日志统一使用 `in_memory`。SDK 版本仅用于响应表示。[上游修正](https://github.com/openai/openai-python/commit/f9d2d1359688a6247ecba858fc687173c480c9c8)

Responses 已按客户端版本处理缓存驻留字段。Python SDK 2.8.1 严格模式可读取原先解析失败的历史响应；2.8.1、3.42.1、3.55.1 的默认与严格模式共六组均可交给 Node 续读。五份新响应跨版本创建、重启后读取，落盘记录的 SHA-256 全部一致。CPU 验证已完成，原生 TLS 真实模型和发行待验。[CPU 验证](evidence/serve-response-cache-retention-20261127/cpu.json)

### 成功响应的跨客户端游标续读

官方 AsyncOpenAI 2.8.1、3.42.1 和 3.55.1 严格模式均在两份真实历史回答的正文中段关闭了流。每次创建新客户端，使用原游标分别收回 14 和 18 个事件，逐片拼回原文。高级接口取得相同终态，其他客户从同一游标读取均返回 404。此次使用 CPU 回放服务。[异步验收](evidence/serve-response-cache-retention-20261127/async-cursor.json)

两份真实成功响应通过原生 CPU HTTP 服务交给官方 SDK。Node 7.47.0 从 Python 提供的游标 14 和 17 分别读到余下 14 和 18 个事件；重启测试服务后得到相同正文，越界游标返回 400，其他客户读取返回 404。Python 三个版本的六种配置中五种通过。2.8.1 严格模式在 GET 解析时报缓存枚举错误，已保存三份原始响应的定位证据。以上为修复前的记录。[兼容记录](evidence/serve-video-layer-composition-20261127/sdk-cursor.json)

Node 的底层迭代器用服务端游标读取后续事件；高级接口 `finalResponse()` 读取全部事件来重建最终对象，并向监听器交付游标之后的事件。两条路径的序号、文本增量、消息 ID 与终态均与原流一致。两个客户端收到的原始 GET 正文已逐字节核对。

### Managed Agents 会话的配额失败

Node 7.47.0 经原生 HTTPS 开启会话事件订阅。发送第二个回合返回 202，订阅随后收到 `agent.session.turn.failed`，错误码为 `rate_limit_exceeded`。会话回到 `idle`，配额窗口恢复后使用同一会话得到 42。[设备验收](evidence/serve-responses-errors-20261126/agent-turn-http.json)

| Python SDK | 客户端入口 | 默认模式和严格模式的结果 |
|---|---|---|
| 3.42.1、3.55.1 | `beta.agents.sessions` | 会话、轮次、轮次列表和五个事件的公开字段一致。另一客户查询该轮次得到 404。 |
| 2.8.1 | 通用 `get` 与 `openai.Stream` | 同样的 JSON、事件和 404 可读。此版没有 `beta.agents.sessions` 类型化入口。 |

旧版通用调用须带 `OpenAI-Beta: agents=v1`。传入 `cast_to=dict[str, Any]`，订阅时另传 `stream=True`、`stream_cls=openai.Stream[dict[str, Any]]` 和 `Accept: text/event-stream`。2.8.1 默认模式在收到合法的会话 JSON 后，使用裸 `dict` 会抛出 `ValueError`。通用调用返回普通字典。[可运行的客户端示例](../tools/serve_audit/agent_turn_quota_sdk.py)

Python 验证使用原设备业务的对象副本。原生 HTTP 处理器重新读取四份响应，事件投影与 Node 当时收到的五个事件逐项核对 ID 和类型。使用相应版本的解释器运行 `python -I -B tools/serve_audit/agent_turn_quota_sdk.py`，加 `--strict` 可复核该 SDK 的严格模式。[六组验证结果](evidence/serve-responses-errors-20261126/agent-quota-python.json)

### 工具响应的严格客户端

三版严格 Python SDK 读取了五份真实工具响应。普通函数的 JSON、成功 MCP 的 JSON 和 SSE 均通过。原普通函数是未开启流的后台请求，重放按合同返回 400。2.8.1 在失败 MCP 的错误对象和程序化工具类型上仍无法解析。3.42.1、3.55.1 均通过相应 JSON 和 SSE。旧版错误字段保留全部内容转为 JSON 字符串后，独立 SDK 实验可读回原错误和 38 个事件，原生实现待补。[原始记录和类型定义](evidence/serve-response-cache-retention-20261127/tool-sdk-audit.json)

### Assistants Runs 与工具回填

实测任务让模型调用 `get_release`，从项目发布记录取出版本号 460，按原工具调用 ID 回填并取得最终答案。下表对应同一个真实任务。[原生 TLS 验收](evidence/serve-assistants-20261124/runs.json)

| 操作 | Node 7.47.0 访问原生 TLS | Python 2.8.1、3.42.1、3.55.1 解析留存响应 |
|---|---|---|
| 创建 Run 的流 | 9 个事件，终态为 `requires_action` | 默认和严格模式逐字段一致，可取得原工具调用 ID、函数名及参数 |
| 回填工具结果的流 | 13 个事件，终态为 `completed`，消息正文为 460 | 两种模式均读到原工具步骤完成事件、工具输出与最终消息 |
| 八项 JSON 操作 | 留存 36 个成功响应体，包含创建、查询、取消、列表、更新和步骤读取 | 三版严格类型检查已覆盖这批响应。历史记录只保留了成功状态范围 |
| 错误请求 | 三次错误回填均为 400，跨客户读取 Run 和步骤均为 404，执行中写线程为 409 | 本次 Python 回放只使用了成功请求的数据 |

Python 的 `get_final_run_steps()` 只收集本次订阅收到创建事件的步骤。回填订阅中可从 `thread.run.step.completed` 取得前一个订阅已建立的工具步骤，也可以用 `runs.steps` 按 ID 查询。[JSON 回放](evidence/serve-stored-message-projection-20261125/python-resource-replay.json) · [流回放、版本和输入摘要](evidence/serve-assistants-20261124/python-streams.json)

在留有原始验收目录的设备上，用对应版本 OpenAI Python SDK 的解释器执行下列命令。脚本先检查回执与原始事件的摘要，再把 22 组已解析的事件名和数据编码为 SSE，交给 SDK 的公开流接口。添加 `--strict` 可检查该版本的严格响应验证选项。

```sh
python -I -B tools/serve_audit/assistant_run_sdk.py --workspace /path/to/acceptance-workspace
```

### Responses 失败回包

失败资源和终态事件使用官方枚举表达错误类别。例如，前台生成中途被显式取消，客户收到 `response.failed`、`error.code = server_error` 和 `error.type = explicit_cancel`。原消息保留。正常取消后台作业继续返回 `status = cancelled` 和空错误。内部恢复逻辑按原始故障名分流。

原生 CPU 程序输出的 5 个事件和对应 JSON 已用 Python 2.8.1、3.42.1、3.55.1 的默认和严格模式重放。三个 SDK 的错误模型均拒绝原来的 `explicit_cancel` 公开代码。读取历史持久记录时按同一规则投影，原记录仍可由旧包读取。[测试数据与摘要](evidence/serve-responses-errors-20261126/cpu.json)

```sh
python -I -B tools/serve_audit/response_failure_replay.py --strict
```

真实 TLS 短验一次启动，用时 37.324 秒。Python 3.55.1 严格模式读取旧 Beta 故障、重复取消并从原游标续读，均返回 200。实际前台生成取消后，已编号的正文增量全部送达，最后一个事件为 response.failed。三个响应正文和两个解码后的失败终态均通过官方 HTTP 及本地合同校验。其他客户读取得到 404；原会话续答和独立 Chat 均回答 42。下一步验证主、轻配置接续以及主包与回退包恢复。[验收记录](evidence/serve-responses-errors-20261126/business.json)

Node 7.47.0 对同一份原生五事件流分别使用 responses.create 的异步迭代器与 responses.stream().finalResponse()。两者均保留失败终态、工具调用 ID 和未完成参数，GET 与终态一致。按游标 3 恢复时只送达序号 4，游标 4 处没有新事件；两次均能取得完整的失败对象。复验命令为 `node tools/serve_audit/response_failure_replay.mjs <SDK安装目录>`。[Node 回放](evidence/serve-responses-errors-20261126/node-aggregate.json)

rev530 已发行 Responses 失败分类、取消流序号和旧 Beta 工具输出修复。分别终止模型并触发主网关致命退出后，严格客户端均用原 ID 和游标读回相同的失败正文与终态，跨客户访问为 404，独立 Chat 答 42。主包与回退包各用同一组 36 份实际回包及终态校验封包合同。默认意图保持 STOPPED，发行启动模型 0 次，13 份客户与恢复记录保持完整。[发行与回退记录](evidence/serve-responses-errors-20261126/delivery.json)

### 获取失败流的最终结果

| SDK 与调用 | 收到 response.failed 后 |
|---|---|
| Python 2.8.1、3.42.1、3.55.1，responses.stream().get_final_response() | 抛出 RuntimeError。迭代事件时读取 event.response，或使用 responses.retrieve(id)。 |
| Node 7.47.0，responses.stream().finalResponse() | 返回 status 为 failed 的响应，保留未完成的工具参数。 |

两端的高级恢复方法均拉取完整事件流，再按 starting_after 向调用方分发。游标已在终态时，Python 客户端可用 GET 读取已存结果。[六种 Python 配置](evidence/serve-responses-errors-20261126/python-aggregate.json) · [Node 回放](evidence/serve-responses-errors-20261126/node-aggregate.json)
