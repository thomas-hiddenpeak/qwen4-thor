# Live WebRTC 独立媒体进程

2026-11-23。Thor 本机两个独立进程通过 UDP 回环完成 ICE、DTLS/SRTP、SCTP 连接。C++ 工作进程从继承的 Unix 顺序包套接字接收 SDP offer，在显式网卡收集完成后返回 answer，授权方收到证书核对成功事件后才可交换业务数据。对端使用独立的 GStreamer Python 绑定。详见[结构化结果](result.json)。

- Opus 使用协商的动态载荷号 111，G.711 µ-law 和 A-law 分别使用 0 和 8。每种编解码器均解码接近 1.2 秒的双向 PCM。输入为 400 Hz，输出为 850 Hz，在两个方向各自核对频率与幅度。双向控制消息成功，随后关闭数据通道，语音仍能通过。
- 证书不匹配时，父进程收到失败，未收到已验证媒体或数据通道就绪事件。缺失插件、超大数据包、畸形 SDP、无安全 RTP、乱序命令、无效本地地址和所有者退出均受到有界处理。无人提交 offer 时，15 秒后回收工作进程。IPC 每个方向按包限长，接收暂存上限 128 条、256 KiB。
- 本机已有 GStreamer 1.24.2、音频及 WebRTC 运行库。验证用的 ARM64 开发头和 NICE 插件经系统软件包索引 SHA256 核对后解包至忽略目录；系统未安装或替换软件。核心程序的动态依赖中没有 GStreamer、libnice 和 SRTP。UPnP 已显式关闭，返回的候选只来自所选接口。
- 原始验证曾发现 Python 绑定的描述符生命周期、ICE 自动候选和跨线程获取传输属性问题，已修正。最终运行将 GLib critical 视为致命错误。没有加载模型，尚待 Live HTTP 创建、真实模型音频、订阅旁路和可回退交付。协议参考 [GStreamer webrtcbin](https://gstreamer.freedesktop.org/documentation/webrtc/index.html) 和 [OpenAI Live 会话](https://developers.openai.com/api/docs/guides/live-conversations)。
