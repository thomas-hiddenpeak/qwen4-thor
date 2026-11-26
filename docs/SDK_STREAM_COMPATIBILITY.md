# 官方 SDK 的流式兼容记录

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
