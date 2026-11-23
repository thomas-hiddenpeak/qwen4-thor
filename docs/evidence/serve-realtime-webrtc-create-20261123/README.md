# Realtime WebRTC 建连与通话

Thor 上固定 JS SDK 7.47.0 通过原生 TLS 请求两次真实 Opus SDP 协商。第一次仅提交 SDP，取得相对 Location 后即挂断，媒体进程确认排空并返回 200。第二次通过 multipart 指定 Realtime 会话配置，独立对端完成 ICE、DTLS、SRTP 与 SCTP。所属客户通过 SDK 旁路读到主端同一会话 ID 和指令。其他客户的旁路和挂断都得到 404。通话中再次创建得到 429。[实机验收](http.json)

对端播放录音“请查询当前项目版本号。”，收到正确转录、完成状态的命名工具及参数 `{"record_key":"release"}`。客户端读取冻结的项目状态，回填后收到语音转录 rev426 及同一响应的开始、结束播放事件。对端实际解码 53,248 个 PCM 样本，峰值 14,011。数据通道的音频 delta 事件数为零。挂断得到空正文 200，再次挂断得到 404，Chat 回答 42。1 次模型启动，67.30 秒，65 次 NVML 查询正常，资源和 GPU 租约均已释放。[操作次数与构建身份](qualification.json)

27 项原生用例覆盖 SDP 长度、重复字段和错误类型、认证前控制、SCTP 和旁路共用会话、关闭数据通道及既有的电话打断和 Live 前端。6 项公开合同检查通过。原生入口首轮误用输出文件名作为 Make 目标，旧二进制对新路由返回 404；改用实际目标 `q4t_ingress` 重建后，原生及可选 nginx 均转发了真实 HTTP 的 SDP 正文和 Location。[CPU 结果](cpu.txt) · [首轮错误](original-native-route.txt) · [路由审计](inventory.json)

业务验收范围为本机回环 Opus、长期服务端客户凭据和原生 TLS。跨网 ICE/TURN、Realtime 临时客户端凭据与组合发行另行验收。默认版本 rev426。一次此前电话工具返回 502 的原始失败仍在 A10 排查。[通话驱动](../../../tools/serve_live/realtime_webrtc_acceptance.py) · [SDK 用法](../../SERVE_LIVE_2026-11-21.md)
