# 同一来电的 Realtime 与 Live 通知和拒接

2026-11-23，Thor 本机模拟运营商发起一条实际 SIP/mTLS INVITE。部署订阅两种事件，独立 HTTPS 接收端使用官方 OpenAI JS 7.47.0 验签。两个通知拥有不同的事件 ID 和客户操作 ID，同一原始 Call-ID 与完整头字段对应同一电话事务。Realtime 通知只包含 `data.call_id` 和 `data.sip_headers`；Live 通知使用 `data.session_id` 和传输类型。

| 实际操作 | 对端结果 |
|---|---|
| 使用其他客户 API Key 在原生 TLS 入口拒接 | HTTP 404 |
| 来电所属客户提交无效的 299 | HTTP 400，来电仍可处理 |
| 官方 SDK 调用 `realtime.calls.reject(call_id)`，省略参数 | SIP 对端收到 180 和默认的 603 Decline。暂缓 ACK 期间 SDK 仍在等待，对端确认后 HTTP 返回空正文 200 |
| 再从 Live 操作原 `session_id` | HTTP 404，事务和两个操作 ID 已一起注销 |
| 同一模型完成独立客户 Chat | 正常回答 42，执行槽释放；两次通知成功，通知失败计数为零 |

[真实验收及二进制身份](acceptance.json)记录 36.08 秒、1 次模型启动、4 次 SDK 调用、1 条 Chat，严格 GPU 租约的 36 次 NVML 查询均成功，退出及端口回收正常。未切换默认 rev426。

另有 4 项实际所有者 CPU 测试覆盖单独订阅 Realtime、只收到 Live 的既有配置、双订阅下未获 ACK 的 504、另一入口竞争返回 409 和迟到 ACK 后双 ID 注销。6 项发送进程测试覆盖两个事件独立确认及显式重试，其中 Live 首次投递 503、Realtime 投递 204 后仍可补投 Live 原事件；已成功的 Realtime 事件不会重复发送。3 项原生输入/准入、5 项公开合同、可选 nginx 路由转发和清单 36 项变异检查通过。原始结果在 `.q4t-work/serve-live-20261123/sip-webhook/realtime-*` 和 `sip-realtime-business-01/`。

[电话部署与订阅配置](../../SERVE_LIVE_2026-11-21.md#在原生-serve-中启用入站电话)。接听、双向安全语音、REFER 与挂断仍属于待实现和验收的通话生命周期。
