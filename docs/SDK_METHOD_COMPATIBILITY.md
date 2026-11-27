# 官方 Python 与 Node SDK 的方法兼容性

选择 SDK 版本时，可在[逐方法 CSV](evidence/serve-sdk-method-matrix-20261127/methods.csv)中按 HTTP 方法和路径查找。每行给出两个官方客户端的资源方法名和六个版本各自的调用方式。路径保留 `?beta=true` 等选择器，使用时在服务的 `/v1` 基地址下展开。

| SDK | 使用当前路径的资源方法 | 使用旧取消路径的资源方法 | 通过通用 HTTP 方法调用 |
|---|---:|---:|---:|
| Python 2.8.1 | 123 | 1 | 107 |
| Python 2.33.0 | 139 | 1 | 91 |
| Python 3.42.1 | 231 | 0 | 0 |
| Python 3.55.1 | 231 | 0 | 0 |
| Node 6.34.0 | 138 | 1 | 92 |
| Node 7.47.0 | 231 | 0 | 0 |

CSV 中的 `resource` 可以直接使用对应的资源方法。`generic` 表示该版本没有表中列出的资源方法，可以改用客户端公开的 HTTP 方法。例如查询 Webhook 事件类型时，Python 可调用 `client.get("/webhook_event_types", cast_to=dict)`，Node 可调用 `await client.get("/webhook_event_types")`。具体请求体和成功响应见随包 OpenAPI；通用方法的调用方负责按合同读取结果。

`legacy_cancel` 仅涉及 `evals.runs.cancel`。旧客户端将 POST 发往 `/evals/{eval_id}/runs/{run_id}`，新客户端在路径末尾加上 `/cancel`。q4t 已在真实 Eval Run 上验证旧路径、当前路径及另一客户的 404。 [取消业务回执](evidence/serve-sdk-routes-20261126/business.json)

## 验证范围

- **路由与错误。** 使用候选发行包的原生入口，在隔离的本机环境实际采集了 232 个缺少凭据的 HTTP 响应，覆盖 231 个方法及旧取消路径。随后让六个未修改的 SDK 按各自方式产生请求，逐个匹配已采集的目标，并解析返回的 `401`。1,386 次 SDK 调用均读回了 `AuthenticationError`、错误字段、请求 ID 和 Bearer 认证挑战。采集时后端收到的请求为零。成功业务、鉴权通过以后的路由和请求体验证各有独立验收。 [采集回包](evidence/serve-sdk-method-matrix-20261127/challenges.json) · [版本统计及摘要](evidence/serve-sdk-method-matrix-20261127/matrix.json)
- **Webhook 事件目录。** 候选引擎真实直连返回带 Bearer 认证挑战与请求 ID 的 401；同一实例经原生 TLS 提供事件目录，六个官方 SDK 均读回相同的 23 个事件类型。已对收到的错误与目录分别核对官方和项目 OpenAPI。启动一次，生成请求零条。 [设备回执](evidence/serve-sdk-method-matrix-20261127/foreground.json)
- **工具、流式和恢复。** Responses 的真实工具回填、已存事件续读及故障后接管已有按版本的证明。服务对 SDK 投影的差异、适用接口和限制记录在流式兼容说明中。 [Python 工具恢复](evidence/serve-program-sdk-reader-20261127/delivery.json) · [Node 工具恢复](evidence/serve-node-sdk-presentation-20261127/delivery.json) · [流式兼容说明](SDK_STREAM_COMPATIBILITY.md)
- **重试。** 准入前的配额或恢复错误，以及已经转交写请求后丢失应答的错误，各有原生和可选 nginx 入口的独立 SDK 记录。 [重试证据](evidence/serve-inference-http-errors-20261127/sdk-retries.json)

复核版本时，Python 使用已安装目标 SDK 的解释器运行 `-m tools.serve_audit.sdk_method_matrix <challenges.json>`。Node 使用 `node tools/serve_audit/sdk_method_matrix.mjs <openai 包目录> <challenges.json>`。将每个输出按 `python-版本.json` 或 `node-版本.json` 放入同一目录，再运行 `python3 -m tools.serve_audit.render_sdk_matrix <challenges.json> <客户端输出目录> <矩阵输出目录>`。

## Live 录音下载及标准响应头

临时 503 给出至少一秒的等待时间。Python 3.42.1、3.55.1 和 Node 7.47.0 可通过 Live 资源方法下载；矩阵中的三个较早版本使用公开的通用 GET。六版均通过原生报文回放取得同一 WAV。[客户端记录](evidence/serve-sdk-method-matrix-20261127/recording-download-retries.json)

本次冻结的官方规范明确列出 13 个「方法、状态码、响应头」组合。已声明的方法覆盖其中 10 个。其余三个属于图像变体接口的 429 图片配额响应头，随该后端待交付。语音响应仅在实际分块传输时携带 Transfer-Encoding。向量库的普通冲突可省略 Retry-After。[逐项核对](evidence/serve-sdk-method-matrix-20261127/response-headers.json)

在对应的 Python SDK 环境执行 `python -m tools.serve_audit.recording_download_retry`，或执行 `node tools/serve_audit/recording_download_retry.mjs /path/to/openai`，可离线复核该版本的等待时间、请求次数、请求 ID 和音频摘要。
