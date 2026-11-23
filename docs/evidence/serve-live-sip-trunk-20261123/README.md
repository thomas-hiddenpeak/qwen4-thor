# 已认证 SIP 中继来电与拒接回执

2026-11-23，在 Thor 的回环地址上运行独立的 `q4t-live-sip` 进程，用 Python TLS 客户端直接收发 SIP/2.0。第一轮 3 项通过；加入服务端私钥检查与生命周期用例后的 [6 项结果](peer-tests.txt) 可复查。0 次模型启动。

| 实际情形 | 私有控制端及电话对端所见 |
|---|---|
| 合法客户端证书、伪造 From/To 的另一个租户名 | 只产生配置的客户身份和随机 Live ID，电话对端收到 180 |
| 未提供证书、同 CA 签发给另一中继、不同 CA 签发给同一中继 | 无来电事件；随后合法中继正常 |
| 不同身份或错误拒接码 | not_found 或 invalid_status；原事务仍能正常拒接 |
| 重发 INVITE、两次拒接、ACK | 相同 Via branch、Call-ID、CSeq、To tag；回复 486，重复拒接收到 conflict，对端 ACK 后回收 |
| 一个 TLS 连接上的两路来电 | 无对应事务的 CANCEL 得到 481；取消第一路得到 200/487，第二路独立得到 603，两笔 ACK 各自关联 |
| 30 秒无人处理、控制套接字关闭、服务证书与私钥不匹配 | 480 及真实 ACK；未完成来电收到 503 并退出；错误密钥启动失败 |

信令使用 [Sofia-SIP NTA](https://sofia-sip.sourceforge.net/refdocs/nta/) 的状态事务与 [中继 TLS 验证](https://sofia-sip.sourceforge.net/refdocs/tport/tport__tag_8h.html)。仅电话进程动态链接 Sofia-SIP（LGPL-2.1-or-later）和系统 OpenSSL。Ubuntu noble arm64 的开发及运行包私下解包，版本 `1.12.11+20110422.1+1e14eea~dfsg-6.1build2`；无系统安装。两个 deb 的 SHA256 依次为 `7d98da7dd0edee0f14fe2615e6329e7e24fb45a5480b06b505650a094a791353`、`9817628a9626723b9e0a067af3e4ce791dbc43424596498ba9010f570c997b21`。原始构建与试验记录在 `.q4t-work/serve-live-20261123/sip-signal/`。

进程通过继承的 UNIX SOCK_SEQPACKET 与所有者通讯。每个监听地址绑定一个受信 CA、中继证书主体和客户身份，最多持有 64 笔来电。信令提交与远端 ACK 是独立事件。承载进程限制主动 connect，防止原连接断开后按来电的 Via 地址另行拨出。此阶段验证独立进程；公开 webhook、电话 HTTP 操作、接通后的 SRTP 和模型资料查询继续推进。
