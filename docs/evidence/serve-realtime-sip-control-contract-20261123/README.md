# Realtime 来电控制输入合同

原生服务直接从固定的 OpenAI Node SDK 7.47.0 提取 `CallAcceptParams`、`CallReferParams` 和 `CallRejectParams`。工作分支中的 Realtime 拒接、转接入口和发行用 OpenAPI 分别使用 Realtime 自身的声明。两个 SDK 共用 SIP 状态码和 URI 规则。接听的输入校验已准备好，独立 Realtime 接听和会话旁路尚待接入。

在 Thor 原生 TLS 上，官方 SDK 使用同一通待处理的 SIP 来电完成七次 HTTP 调用。其他客户拒接得到 404。所属客户提交 299 或 `{status_code:null}` 均得到 400。带残缺百分号转义的转接地址得到 400；合法地址因来电尚未接通得到 409。随后所属客户调用 `realtime.calls.reject(call_id, null)`，中继实际收到 603；等待远端 ACK 后 SDK 收到空正文 200。来电注销后以另一 Live ID 操作得到 404。普通 Chat 回答 42，所有自有资源已释放。一次模型启动用时 37.49 秒，37 次 NVML 采样全部成功。[业务回执及构建身份](acceptance.json)。

固定版 TypeScript 编译器检查 492 个 SDK 样例，其中 83 个接受、409 个拒绝，原生结果零差异。另测接听配置顶层类型、受支持的音频空值、只读 ID、拒接码边界、错误转接 URI。6 项既有 Live SIP 用例和 5 项公开合同检查通过。打包与独立 OpenAPI 均从相同公开定义生成。[SDK 结果](oracle.json)、[原生合同](native.txt)、[Live 回归](live.txt)、[公开合同](public-tests.txt)、[来源审计](inventory.json)。

本轮的合法转接请求只验证待接来电的状态边界。通话接通后的真实 REFER、远端失败或受理及后续目标状态已有[独立中继证据](../serve-live-sip-transfer-20261123/README.md)。后续接听必须连接真实 Realtime 会话、已认证媒体、客户所有权和 `call_id` 旁路，再验收其语音和工具业务。
