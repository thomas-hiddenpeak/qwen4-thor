# 原生 SIP 接听、传音和挂断

2026-11-23，Thor 上运行正式构建的信令与媒体进程。独立电话对端通过相互认证的 TLS 与本机中继通话。其 RTP 对端只使用从电话 200 响应实际收到的 SDP、端口与密钥。测试所有者把已确认的 ACK 传给媒体进程，尚待接入公开 HTTP 接听与模型会话。

| 电话行为 | 实际回执 |
|---|---|
| INVITE、180、接听、重传 INVITE、新事务分支的 ACK | 前后两份 200 携带相同的媒体应答和 To tag；按 Contact 确认后仅产生一次 ACK 事件 |
| 确认后 G.711 双向加密通话，电话对端挂断 | 对端与所有者分别收到 720 Hz、440 Hz，加密 RTCP 回执匹配；BYE 获得 200，释放两个媒体端口 |
| 外客户接听、错误会话标签、取消抢先、确认前对端挂断 | 外客户查无此呼叫，错误标签的 BYE 收到 481；取消产生 487；确认前结束的通话没有被激活 |
| 本地所有者挂断 | BYE 经原 TLS 连接到达对端。100 临时答复后仍等待；200 或 481 逐字保留，结束事件与远端应答分别记录 |
| 对端读到 BYE 后断开 TLS，不回最终答复 | 所有者收到 LOST，没有成功回执 |

[组合回环 24 项](signal-media.txt)在 36.771 秒内通过，其中 9 项原有中继检查、9 项媒体检查与 6 项新对话检查。随后给信令进程关闭 core dump，正式产物 SHA256 为 `7ee897e5129e12302d063e7d811087e08c5f0ce20a224b62d983eb9c50e75c0f`，[6 项对话复验](final-dialogs.txt)为 1.785 秒。媒体产物身份与[独立媒体验收](../serve-sip-secure-media-20261123/README.md)相同。未启动模型。

复验时给系统 Python 提供 pylibsrtp，分别设置 `Q4T_SIP_WORKER`、`Q4T_SIP_MEDIA_WORKER` 为构建的两个进程路径。执行 `/usr/bin/python3 -B -m unittest -v tools.serve_live.test_sip_dialog`。每次测试生成独立的证书与密钥，监听回环临时端口。Sofia-SIP 负责 SIP 对话标识、事务与路由，主动 BYE 只使用原有已认证连接。[Sofia-SIP NTA](https://sofia-sip.sourceforge.net/refdocs/nta/nta_8h.html)。
