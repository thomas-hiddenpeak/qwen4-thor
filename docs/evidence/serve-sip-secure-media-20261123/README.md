# 电话安全媒体双向验收

2026-11-23，Thor 正式构建目标 `q4t_sip_media`，产物 SHA256 为
`8733022910a465c3fb70643ba6a62c751d6aae1dfad62f8124ea8f99a1118a69`。
该阶段覆盖独立媒体进程和本地 UDP 电话对端，HTTP 接听到模型的业务闭环待验。

| 实测 | 回执 |
|---|---|
| 正式构建，9 项通过，3.488 秒 | [原始输出](engine-peer.txt) |
| 三种编码器各 8 次通话排空及端口重绑，24 次通过，13.610 秒 | [连续关闭记录](drain-all.txt) |
| 共享编码表后 WebRTC 的双向音频、数据通道与单独关闭 | [三种编码器的采样计数与信号幅度](webrtc-codecs.json) |

独立对端用 pylibsrtp 和系统 libopus 解密；G.711 在测试端用 Python 3.12 的 audioop。对端发送 440 Hz、所有者发送 720 Hz，分别检查正确频率和干扰频率的幅度。对端的加密 Sender Report 在收到的加密控制回包中匹配到同一 SSRC 和 LSR。覆盖 Opus、PCMU、PCMA、独立与复用 RTCP、IPv6 静态载荷映射、错误密钥、错误源端口、重放、确认前音频、单向协商、缺失插件及挂断前的尾帧。测试只访问本机回环地址。

最初一次关闭卡在 GStreamer 管线退出。`wait-on-eos=false` 单独使用后，Opus 仍出现一次超时。修复同时结束三个输入分支，在切到 NULL 前发出 flush。另一项最初失败来自 GStreamer 对无 rtpmap 的静态 G.711 载荷没有提供 encoding-name，已按明确映射补齐。原始失败与定位保存在 `.q4t-work/serve-live-20261123/sip-media/{peer-01,edges-01,edges-02,drain-timing}.log`。

在 Thor 上给正式构建提供 libSRTP2 开发头，构建 `q4t_sip_media`。使用含 pylibsrtp 的系统 Python，把 `Q4T_SIP_MEDIA_WORKER` 设为该产物的绝对路径，再执行 `/usr/bin/python3 -B -m unittest -v tools.serve_live.test_sip_media`。测试用开发包与 Python 包仅解压在工作目录。运行时只有电话媒体进程使用 libSRTP2 和系统 GStreamer；测试和验收记录均不保存密钥或带密钥的 SDP。
