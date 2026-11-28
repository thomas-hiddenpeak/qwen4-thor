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

## 按方法查真实业务回包

业务矩阵按 231 个公开方法和六版 SDK 列出实机回包。例如 `stream-gateway-restore/200=1` 表示该版本客户端在网关切换后，用原响应 ID 请求成功一次。空白表示所收录的原始记录未覆盖该组合。各组的发行身份、传输方式、故障阶段和原始凭据可从来源清单核对。[业务矩阵](evidence/serve-sdk-business-index-20261128/business.csv) · [来源清单](evidence/serve-sdk-business-index-20261128/sources.json)

来源清单的 `methods_with_success` 和 `method_sdk_pairs_with_success` 分别给出有成功回包的方法数、方法与版本组合数。每组请求数记录在 `sources` 下。旧上传记录中缺少查询串的请求保留在 `unassigned`。Responses 流式短验的七次原生 TLS SDK 请求和六次托管恢复 SDK 请求均可追溯到原始请求 ID；两次直接 HTTP 对照单独记录。可以用 `python3 -B -m tools.serve_audit.sdk_business_evidence build/sdk-business` 重建清单。

## 验证范围

- **已存 Chat。** 四版 Python 和两版 Node 各将一份真实发行记录存为 Chat，由另一版 SDK 读取同一 ID、更新并按元数据筛选、分页读回原始消息和删除。六个方法在六个版本上都有成功回包。六次跨客户读取和六次删除后读取都返回 404。[实机回执](evidence/serve-stored-chat-sdk-20261128/business.json) · [请求与回包](evidence/serve-stored-chat-sdk-20261128/requests.json)
- rev590 的原生 TLS、嵌入模型和文本模型已通过官方 SDK 批量上传与自动轮询验收。Python 2.8.1 和 Node 7.47.0 各上传两份真实发行记录，另一个版本的客户端通过游标逐页读回原文件、核对内容和过滤检索。Responses 的 file_search 命中 rev588 的原 File ID，模型回答中包含该发行号。两次跨客户访问均返回 404。共 50 个真实请求、一次模型启动。 [实机业务回执](evidence/serve-vector-batch-sdk-20261128/business.json)

- **Uploads 和 Files 接管。** 六版客户端共同操作同一批资源，覆盖四种 Uploads 和五种 Files 方法。主包接受乱序传输、按指定顺序完成；回退包沿用原分片和 File ID，核对下载、归属和删除，再由 Responses 读取实际文件。每版具体经手的方法、HTTP 状态及请求 ID 都在[逐次记录](evidence/serve-upload-sdk-recovery-20261128/requests.json)。

- **Conversations 的八个方法。** 表中六个版本都通过原生 TLS 完成了会话和条目的创建、读取、更新或删除，SDK 自行生成第二页游标并读到了另一个条目。每版还检查了其他客户的密钥、已删条目和已删会话的 404。Python 3.55.1 与 Node 7.47.0 另经 Responses 读取自己的两条会话记录，答出设备实际的版本登记号 223。主包 rev588 的引擎和入口使用私有对象目录与本机测试准入控制器。 [74 个请求](evidence/serve-conversation-sdk-business-20261128/requests.json) · [包身份与验收范围](evidence/serve-conversation-sdk-business-20261128/business.json)
- **路由与错误。** 使用候选发行包的原生入口，在隔离的本机环境实际采集了 232 个缺少凭据的 HTTP 响应，覆盖 231 个方法及旧取消路径。随后让六个未修改的 SDK 按各自方式产生请求，逐个匹配已采集的目标，并解析返回的 `401`。1,386 次 SDK 调用均读回了 `AuthenticationError`、错误字段、请求 ID 和 Bearer 认证挑战。采集时后端收到的请求为零。成功业务、鉴权通过以后的路由和请求体验证各有独立验收。 [采集回包](evidence/serve-sdk-method-matrix-20261127/challenges.json) · [版本统计及摘要](evidence/serve-sdk-method-matrix-20261127/matrix.json)
- **Webhook 事件目录。** 候选引擎真实直连返回带 Bearer 认证挑战与请求 ID 的 401；同一实例经原生 TLS 提供事件目录，六个官方 SDK 均读回相同的 23 个事件类型。已对收到的错误与目录分别核对官方和项目 OpenAPI。启动一次，生成请求零条。 [设备回执](evidence/serve-sdk-method-matrix-20261127/foreground.json)
- **工具、流式和恢复。** Responses 的真实工具回填、已存事件续读及故障后接管已有按版本的证明。服务对 SDK 投影的差异、适用接口和限制记录在流式兼容说明中。 [Python 工具恢复](evidence/serve-program-sdk-reader-20261127/delivery.json) · [Node 工具恢复](evidence/serve-node-sdk-presentation-20261127/delivery.json) · [流式兼容说明](SDK_STREAM_COMPATIBILITY.md)
- **重试。** 准入前的配额或恢复错误，以及已经转交写请求后丢失应答的错误，各有原生和可选 nginx 入口的独立 SDK 记录。 [重试证据](evidence/serve-inference-http-errors-20261127/sdk-retries.json)

复核版本时，Python 使用已安装目标 SDK 的解释器运行 `-m tools.serve_audit.sdk_method_matrix <challenges.json>`。Node 使用 `node tools/serve_audit/sdk_method_matrix.mjs <openai 包目录> <challenges.json>`。将每个输出按 `python-版本.json` 或 `node-版本.json` 放入同一目录，再运行 `python3 -m tools.serve_audit.render_sdk_matrix <challenges.json> <客户端输出目录> <矩阵输出目录>`。

## Responses 流式助手聚合正文

直接调用 Node 的 `responses.stream().finalResponse()` 时，原版 6.34 与 6.44 能读到消息及 URL 引用，`output_text` 却为 undefined。6.45 已在 SDK 内完成聚合。候选从请求头识别相应旧版 Node，为终态流事件拼接所有消息的 `output_text` 片段；工具输出和拒答文字不参与拼接。六个登记 SDK 加上 6.44、6.45 的边界验证均通过 Thor 流的本机 TLS 回放。候选的实机请求与可回退发行继续验收。 [流式助手回执](evidence/serve-responses-search-stream-sdk-20261128/cpu.json)

## Responses 的失败与截断终态

应用需要区分成功、失败和输出预算耗尽时，直接读取终态事件的 `response`。六个登记版本均能从这里取得 `status`、`error` 和 `incomplete_details`。在真实历史事件的 TLS 回放中，四个 Python 版本的 `get_final_response()` 对失败和截断都抛出缺少 `response.completed` 的异常。Node 6.34 的 `finalResponse()` 在这两种情况下返回 `in_progress`，Node 7.47 返回正确终态。[六版客户端回放](evidence/serve-responses-search-stream-sdk-20261128/terminal-clients.json)

以下示例按已存 ID 重放，创建时的事件流也可采用相同的判断。

```python
terminal = None
with client.responses.stream(response_id=response_id) as stream:
    for event in stream:
        if event.type in ("response.completed", "response.failed", "response.incomplete"):
            terminal = event.response
if terminal is None:
    raise RuntimeError("Response stream ended before a terminal event")
```

```javascript
const stream = client.responses.stream({ response_id: responseId });
let terminal;
for await (const event of stream) {
  if (["response.completed", "response.failed", "response.incomplete"].includes(event.type)) {
    terminal = event.response;
  }
}
if (!terminal) throw new Error("Response stream ended before a terminal event");
```

## Live 录音下载及标准响应头

临时 503 给出至少一秒的等待时间。Python 3.42.1、3.55.1 和 Node 7.47.0 可通过 Live 资源方法下载；矩阵中的三个较早版本使用公开的通用 GET。六版均通过原生报文回放取得同一 WAV。[客户端记录](evidence/serve-sdk-method-matrix-20261127/recording-download-retries.json)

本次冻结的官方规范明确列出 13 个「方法、状态码、响应头」组合。已声明的方法覆盖其中 10 个。其余三个属于图像变体接口的 429 图片配额响应头，随该后端待交付。语音响应仅在实际分块传输时携带 Transfer-Encoding。向量库的普通冲突可省略 Retry-After。[逐项核对](evidence/serve-sdk-method-matrix-20261127/response-headers.json)

在对应的 Python SDK 环境执行 `python -m tools.serve_audit.recording_download_retry`，或执行 `node tools/serve_audit/recording_download_retry.mjs /path/to/openai`，可离线复核该版本的等待时间、请求次数、请求 ID 和音频摘要。

主、轻配置均已通过六版客户端的真实 TLS 下载。每个客户端取回同一份 2,620,472 字节的录音，两个配置均按客户身份隔离。[设备回执](evidence/serve-sdk-method-matrix-20261127/recording-device.json)
