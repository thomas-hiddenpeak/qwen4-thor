# Live 原生应用服务实现计划

总目标保持 active，沿用用户确定的完整推理/应用服务范围。组织管理、
云账单和微调训练不纳入；Live 的媒体、会话和委派属于应用服务。
本阶段编号用于实现/验证/提交，不是总任务终点；所有剩余项目继续。

固定依据为本地官方 OpenAI JS 7.47.0、TypeScript 5.9.3，以及2026-11-21
保存的官方 Live 指南。sources.json记录19个SDK来源（含MIT许可）；
类型图16个根、941个节点、7个实际可达来源，SHA256为
c145a8561370cf58536e63ef941b8fe3a39c0b147f7aa9a136282b11632e81d6。
图只作为输入/输出合同，不能代替执行。运行时不加载Node/TypeScript/SDK。

| 子项 | 实现和出口 |
|---|---|
| L01 合同 | 复用C++有界类型校验器；完整主连接、sideband、fork、HTTP媒体/会话类型根；独立SDK编译器正反例；重复字段、深度/工作量和输出错误边界。还不宣称业务可用。 |
| L02 主WSS | /v1/live/sessions原生握手、鉴权/会话槽；session.start/started和不可变设置；持续PCM16/24k与G.711输入、真实ASR/LLM/TTS、音频/转录增量、mute/unmute、正常排空/异常断开。累计活动会话秒（含静音/等待）与后端token分别计量。 |
| L03 任务与上下文 | frontend/back-end独立指令与上下文所有权；client/Responses/Decisions委派、响应包装、工具回填、触发器与取消；静默/可说上下文、提示追加、初始历史、确认延时和音频fixture。所有模型工作受现有RPM/TPM、公平准入和资源预算约束。 |
| L04 存储与旁路 | 认证scope绑定的sideband、可选store、录音下载、完整历史fork及所有者切换；每连接权限和背压独立；辅助失败不得停止健康核心；不重放旧工具/请求，不伪造已关闭事件。 |
| L05 媒体传输 | 原生WebRTC及SIP会话接入，create/accept/reject/hangup/refer、fork/sideband与控制权限；先审核已有必要系统传输/编解码能力，新增库需说明启用范围和失效影响，nginx/systemd及外部媒体服务不得成为核心强制依赖。真实本地对端短验，外部电信账户结果与本机合同分开。 |
| L06 组合交付 | 标准SDK双向语音与真实项目资料工具闭环，主/轻配置、入口/辅助/核心故障边界与资源清理；可回退包、文档/矩阵、CI、main。旧模型质量失败/听感待验保持；继续所有master缺口。 |

首组仅成组接入L01，冻结4项新原生CPU场景（其中1项对照808个独立SDK正反例）及
2项直接受影响的既有Realtime校验场景。完整SDK图不宣称执行业务通过。
不运行GPU、不做性能宣称。L02/L03接通后冻结对应真实业务短验预算，
按同一严格GPU租约运行；不重复已验身份不变的完整质量/性能矩阵。
协议事件必须来自实际状态、调用和计量；socket关闭不冒充成功排空。

模型适配：复用原生ASR、当前LLM及原生TTS，frontend与delegated backend
可共享物理模型，但保持独立提示、历史与准入。不伪称具有GPT-Live模型
权重或原生端到端语音模型的时延/质量，不能把该区别用作不实现接口的理由。

引用：
- https://developers.openai.com/api/reference/resources/live
- https://developers.openai.com/api/docs/guides/live
- https://developers.openai.com/api/docs/guides/live-conversations
- https://developers.openai.com/api/docs/guides/live-delegation

L01源码已接入工作树；6项针对性原生CPU通过，Live 808与Realtime 476个SDK正反例无差异。首轮独立链接漏掉既有JSON转义函数，原失败保留；补齐链接后通过，运行时未为此改写。尚未接入服务路由或模型业务，默认rev426保持STOPPED，未运行新GPU。PR #57已按冻结head合入main，本组单独提交。

正式宿主构建与生成器一致性已通过，原始结果见[合同CPU证据](evidence/serve-live-contract-cpu-20261121/README.md)。下一组接入独立Live配置/生命周期及实际语音，不把现有Realtime的24k输出限制误用为Live的16k/24k合同。

配置事务9项、16k转换与直接保护12项CPU通过，见[原始证据](evidence/serve-live-configuration-pcm-cpu-20261121/README.md)。真实计数/后端绑定仍在后续L02实现，不能用受控CPU回调宣称模型业务通过。默认原生声线将按实际名称公开，不能把vivian冒充marin等其他模型声线。独立会话关闭/排空、持续输入和真实工具链继续。

独立生命周期10项CPU通过（0.031616秒，正式构建零警告），尚未接实际
模型和路由。启动失败清理、更新事务、正常排空、异常断线、过期、
输出背压及伪造终态均检查。正常WebSocket写出后关闭与工作线程
组合继续。用量依据[官方说明](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live)，
按活动单调时长累计，静音/等待不暂停，快照不求和；不代表云账单。

正常传输24项CPU通过，与生命周期去重31项，见[确切来源和日志](evidence/serve-live-lifecycle-transport-cpu-20261121/README.md)。
真实socketpair验证完整终态后1000关闭，慢读/EOF/撤权不误报成功；
默认与模型均未启动，继续实际后端接入。

[原生连续输入15项CPU](evidence/serve-live-input-cpu-20261121/README.md)闭合：
复用实际音频队列/借用者/历史，评分与识别为CPU夹具。首轮四个
极短输入失败保持；不足一个ASR样本的关闭尾部清理不补样或伪造
转录，8项针对性补验通过。实际LLM/TTS、ASR资产和原生路由继续。

[输出和工作线程10项CPU](evidence/serve-live-frontend-cpu-20261121/README.md)
闭合；首轮头文件/夹具失败原记录保持，4项针对性补验通过。实际
音频回执、格式/时长、有界队列、取消Join和用量所有权已准备；
Live累计83项不同CPU，不等于模型业务验收。继续前台调度、真实
模型绑定和原生路由，然后其余子项及全部master任务。

[前台调度10项CPU](evidence/serve-live-scheduler-cpu-20261121/README.md)
全部通过；实际线程与历史组合、插话准入、关闭/取消、用量与失败
后新轮次均验证，累计93项不同CPU。真实模型回调和路由未接，继续
上下文/委派及主WSS，再按同一严格租约做有界真实业务。

[客户端上下文/委派13项CPU](evidence/serve-live-context-cpu-20261121/README.md)
通过（10新增/3直接保护），累计103项不同CPU。真实计数器接口、持久
指令、静默/可说上下文、实际前台队列、完整工具交接和延迟确认取消
已准备；实际tokenizer/model仍需绑定。内部工具成功只表示创建交接，
不表示外部任务完成；继续WSS、Responses/Decisions、fixture与其余子项。

[主WSS原生绑定12项CPU](evidence/serve-live-binding-cpu-20261121/README.md)
通过，累计112项不同CPU。SDK合法省略启动历史type已兼容，开发者
角色映射为内部应用指令角色，实际提示保留文本与顺序。主连接已绑定
现有tokenizer、当前客户身份与ASR/LLM/TTS原生入口；正式引擎构建与
真实业务短验继续，不把受控CPU执行当实际模型验收。录制/其他委派
及媒体尚待实现；默认rev426保持。

[主WSS客户端委派真实闭环](evidence/serve-live-client-http-20261121/README.md)
已验：ASR查询、真实项目读取/回填、原生语音rev426、关闭及后续普通
Chat均通过。静音尾部额外ASR已修复，原RPM3保持。第三轮最终健康
断言错把正常ASR权重释放当故障，原FAIL保留；5项健康反例及独立
原件复核通过，未追加GPU。累计3启动/10实际工作/255.282秒，默认未改。
L03客户端闭环已验，继续fixture、Responses/Decisions与长上下文保留；
L04—L06及全部master任务仍未完成，不以本组验收作为总目标终点。

[音频片段21项原生CPU](evidence/serve-live-fixture-cpu-20261121/README.md)通过，
其中12新增，累计130项不同原生Live CPU；5项专用驱动反例和正式
引擎构建通过。完整校验、所有权排空、输入丢弃/静音、上下文拒绝、
自然播放与取消边界已实现；真实SDK播放后业务恢复短验继续。

[音频播放实际SDK闭环](evidence/serve-live-fixture-http-20261121/README.md)通过：
1启动/1片段/4工作/68.022秒，逐字节输出、输入丢弃/静音保持、解除后
真实资料闭环、正常关闭及Chat=42均已验。65次NVML正常并释放租约；
累计4启动/14工作/323.305秒。默认未改、旧失败保留，继续其他全部子项。

[服务端 Responses 真实闭环](evidence/serve-live-responses-http-20261123/README.md)通过。客户端文档查询、两轮后台委派、语音结果、配额结算、正常排空和后续 Chat 已核实。语音听感待验，Decisions、录制与媒体继续。

[Decisions 会话条目](evidence/serve-live-decision-items-cpu-20261123/README.md)的原生增删和快照已通过 7 项 CPU 检查。模型评估与触发器继续。

[Decisions 共享请求与答案合同](evidence/serve-decision-plan-cpu-20261123/README.md)已完成针对性 CPU 验证，模型执行和传输验收继续。

[Decisions 原生取分](evidence/serve-decision-inference-cpu-20261123/README.md)已接入 HTTP，真实模型和 Live 委派仍须业务验证。

[Decisions HTTP 业务](evidence/serve-decisions-http-20261123/README.md)已通过。SDK 多题、实际缓存及计量通过；Live 的显式/语音委派、转录触发仍需独立接入和验收。

[L03 Decisions 主连接真实验收](evidence/serve-live-decisions-20261123/README.md)已覆盖手动和语音请求、同会话回填、两种转录触发与资源释放。L04 继续可选录制、下载、旁路和 fork。

L05 的 WebRTC HTTP 创建、受限数据通道和双向 Opus 已通过真实资料查询，详见[验收记录](evidence/serve-live-media-create-20261123/README.md)。L04 的同身份旁路与完整 fork、L05 的 SIP 信令、L06 的发行继续推进。

L04 的可信 WebRTC 旁路已通过同身份实际接入、授权隔离、断开后主通话继续、重连回放与双端正常关闭。L04 继续存储会话 fork，L05 继续 SIP。[CPU 与真实业务记录](evidence/serve-live-sideband-20261123/README.md)。

L04 录制收尾已保存可供子会话读取的私有状态；历史工具与结果保留关联，不保存活动 worker。真实录音及跨身份、容量、重启读取已验证。[保存状态证据](evidence/serve-live-fork-state-20261123/README.md)。后续验收需新会话在真实入口使用这份历史继续业务。

L04 的同身份 fork 已经通过两个真实 SDK 入口。对已完成且存有上下文的源会话，WebSocket 客户端连接 `GET /v1/live/sessions/{session_id}/fork` 后发送 `{"type":"session.start","session":{}}`。WebRTC 客户端向同一路径 POST 新的 SDP offer，成功返回 201。两种方式都获得新的会话 ID。源会话的原始指令、有效指令、资料回填及对话历史随子会话恢复；未设置 store 时继承源配置。WebSocket 默认使用 24 kHz PCM，WebRTC 重新协商媒体并继承或逐项覆盖数据通道权限。曾使用私有工具凭据的 Responses 配置需要在 fork 时提交当前工具配置。旧 WAV 缺少会话状态时仍可下载，fork 返回 409。[实测与边界](evidence/serve-live-fork-http-20261123/README.md)。

L05 待接入 SIP 原生信令、SRTP 媒体、同身份电话控制、DTMF 及对应的真实本机对端。电话协议需要独立于核心服务部署。L06 仍需组合恢复、打包和主线交付。

SIP 接听在构造模型所有者前校验专用 `session.type=live`、启动字段及语音协商边界。拒接、转接与 DTMF 的运行时输入校验已通过 [15 项 CPU 检查](evidence/serve-live-sip-contract-20261123/README.md)。HTTP 回执还需要绑定到真实的来电身份、信令提交与媒体所有者。

L05 独立的入站 SIP 进程已通过双向 TLS 中继身份、并行事务、拒接、CANCEL/ACK 与超时验收。所有者通过私有套接字使用客户身份和 Live ID 处理来电。公开来电通知、接听、SRTP、转接和挂断仍在开发。[回环对端证据](evidence/serve-live-sip-trunk-20261123/README.md)。

L05 来电通知已在事务改写前保存有序字段，过滤 SIP 鉴权，私有 SDP 按同身份读取。慢所有者的积压触发新来电 503，已接纳通话可继续。[本机对端验证](evidence/serve-live-sip-invite-20261123/README.md)。

L05 已从真实来电投递 `live.transport.incoming` 签名 HTTPS 通知。接收端验签、显式重试以及回调与正在进行的电话事务相互独立均已实测。后续需将部署和实际 API 客户接到这条链路。[验收](evidence/serve-live-sip-webhook-20261123/README.md)。

### 在原生 serve 中启用入站电话

为需要电话接入的候选部署构建 `q4t-live-sip`，把它放在 `q4t` 同目录，并让该辅助进程能够加载 Sofia-SIP。启动 `q4t serve` 时传入已有的 `--client-policy-file` 和 `--sip-trunks-file /etc/q4t/sip-trunks.json`。省略电话配置时不会打开电话端口。配置文件须为服务账号所有的私有普通文件（例如 0600）。下例中的地址、证书和密钥为占位值。

```json
{
  "trunks": [{
    "client_id": "alpha",
    "listen_ip": "127.0.0.1",
    "port": 5061,
    "certificate_dir": "/etc/q4t/sip/listener",
    "peer_subject": "carrier.example",
    "webhook": {
      "url": "https://subscriber.example/calls",
      "secret": "whsec_<base64-signing-key>",
      "network": {
        "allow_http": false, "public_networks": false,
        "allowed_hosts": ["subscriber.example"],
        "allowed_networks": ["192.0.2.10/32"], "ports": [443],
        "ca_file": "/etc/q4t/sip/subscriber-ca.pem",
        "timeout_ms": 8000, "max_redirects": 0
      }
    }
  }]
}
```

每个监听端口绑定一个已配置的客户。TLS 目录中的 `agent.pem` 包含监听证书和私钥，`cafile.pem` 保存受信中继 CA。`peer_subject` 约束来电中继的证书身份。通知接收端先用签名密钥核验原始 HTTP 正文与请求头，再用该客户的 API Key 调用 `client.live.sessions.reject(event.data.session_id, {status_code: 486})`。接收端可在拒接完成后返回 204。

拒接码范围为 300–699。收到同一 SIP 事务的 ACK 后 HTTP 返回空正文 200。其他客户或已结束的来电返回 404；已经提交、尚待确认的拒接返回 409；五秒内缺少最终回执返回 504，此时电话对端可能已收到拒接。本机运维端点 `GET /v1/config` 的 `sip` 字段列出已配置与就绪中继数量、启动失败数、成功通知数与通知失败数。接听与挂断见下文；转接与托管发行另有待验收项。

同一中继也可接收 Realtime 来电通知。给上例的 `webhook` 增加 `"event_types": ["live.transport.incoming", "realtime.call.incoming"]` 即可订阅两种事件；仅列其中一种则只投递该事件。旧配置省略此字段时沿用 Live 通知。两种事件带有独立的签名事件 ID、相同的来电头字段和原始创建时间。Live 事件给出 `data.session_id`，Realtime 事件给出 `data.call_id`。接收端将对应字段原样传回各自的接口。

在 Realtime 客户端调用 `client.realtime.calls.reject(event.data.call_id)` 时，中继默认发送 603 Decline；也可显式提交 `{status_code: 486}`。Live 与 Realtime 共享同一 SIP 事务的最终处置和远端 ACK。已经从任一入口发出最终答复时，另一入口的处置返回 409；事务确认、注销后两个 ID 都返回 404。

Realtime SDK 同样允许将整个拒接参数设为 `null`，其效果为默认的 603。显式传入 `{status_code:null}` 会返回 400，来电仍可处置。向待接来电发送合法转接地址返回 409。[真实 SDK 与中继验证](evidence/serve-realtime-sip-control-contract-20261123/README.md)。

电话媒体已支持 RTP/SAVP、SDES 的 AES_CM_128_HMAC_SHA1_80、Opus 和两种 G.711 编码，分别协商收发密钥和 RTCP 端口。已用独立电话对端验证双向音频、远端回执和挂断排空。HTTP 接听与模型会话仍需连通验收。[媒体结果](evidence/serve-sip-secure-media-20261123/README.md)、[SDP 协商](https://www.rfc-editor.org/rfc/rfc3264)、[SDES](https://www.rfc-editor.org/rfc/rfc4568)。

电话中继可在同一 mTLS 对话中发送接听应答、关联远端 ACK，按 Call-ID、双方标签和 CSeq 处理挂断。主动挂断使用原 TLS 连接，记录最终 SIP 状态；对端未给最终答复时单独报告失联。已通过实际信令与媒体组合回环，公开接听、转接及真实模型电话业务继续实施。[对话验收](evidence/serve-sip-dialog-20261123/README.md)。


### 用 Live API 接听并结束电话

签名通知中的 `data.session_id` 对应一笔等待接听的来电。使用同一客户的 API Key，在收到来电后调用固定 SDK 的接听操作。下例选择 vivian 声线并将工具工作委派给客户端。

```js
await client.live.sessions.accept(event.data.session_id, {
  session: {
    type: "live",
    model: "qwen3.8-flash-next",
    audio: {output: {voice: "vivian"}},
    delegation: {type: "client"},
  },
});
```

中继发出 SDP 应答并等待对端 ACK，此后激活媒体，HTTP 返回空正文 200。通过同一 session ID 连接 Live 旁路，可读取语音转录和委派事件，并把查询结果回填。接听占用一个持续到通话结束的实时名额。使用 `client.live.sessions.hangup(id)` 在独立控制名额上挂断；如果中继也发布 Realtime 来电通知，同一客户可用 `client.realtime.calls.hangup(call_id)` 结束该通话。远端 BYE 也会结束会话。

| 调用结果 | 处理 |
|---|---|
| 404 | 该客户没有这笔来电或通话已注销。 |
| 409 | 已有处置占用来电，或通话还未进入可挂断状态。 |
| 接听等待 ACK 超时，504 | 媒体不激活。迟到 ACK 到达后，中继主动发出 BYE。 |
| 主动挂断获得最终 SIP 2xx | HTTP 200，空正文。 |
| 主动挂断获得远端失败状态 | HTTP 502，错误消息保留 SIP 状态码。缺少最终回执返回 504。 |

信令进程和 `q4t-sip-media` 放在模型可执行文件同目录。媒体默认绑定中继的具体监听 IP，仅向本次已认证 TLS 连接的对端 IP 传音。如果媒体中继与信令对端地址不同，或信令监听通配地址，在对应 trunk 增加 `"media": {"local_ip": "192.0.2.10", "peer_ip": "192.0.2.20"}`，两者使用同一地址族的实际单播地址。SDP 必须匹配批准的对端。实际电话对端验证过 Opus 和 G.711；SDK 至真实项目查询的通话使用 Opus。[客户边界与验收](evidence/serve-live-sip-accept-20261123/README.md)。

### 在已接听通话上转接

所属客户可调用 `client.live.sessions.refer(session_id, {target_uri})` 或 `client.realtime.calls.refer(call_id, {target_uri})`。原生控制通道将目标原样放入 Refer-To，通过来电中继的已认证 TLS 连接发送。两个 ID 共享同一笔转接。最终 SIP 2xx 得到空正文 HTTP 200，远端拒绝得到携带 SIP 状态码的 502，五秒截止仍缺答复得到 504。超时后中继可能继续处理，待前一请求或订阅结束才允许重试。

中继通过 NOTIFY 报告 转接目标的结果。`/v1/config` 的 `sip.transfers_answered`、`transfers_failed` 和 `transfers_unknown` 分别累计目标接通、目标拒绝和缺少明确终态。参与方仍可通过 BYE 结束原通话。已验证本机电话对端对两种 SDK 的拒绝、受理、忙线及接通回报。[实测](evidence/serve-live-sip-transfer-20261123/README.md)。

### 在已接听的 Live 电话上收发按键

同客户的 SDK 旁路收到 `transport.dtmf.received` 时，从 `event` 字段读取单个按键。发送示例为 `ws.send({type:'transport.dtmf.send', event:'#', event_id:'ivr-menu'})`。多个按键逐个发送。媒体进程将结束报告写入加密 UDP 套接字后，旁路收到 `transport.dtmf.sent`，其 `client_event_id` 与请求对应。接收端的重传会被去重。

可用按键由电话 SDP 中的 `telephone-event`、事件集合及收发方向决定。未协商的按键会单独返回 `sip_dtmf_not_negotiated`。可选 GStreamer 生成器 `rtpdtmfsrc` 缺失时仍可接听语音；只接收按键的方向可独立工作。密钥、键音数据和按键命令均由电话媒体与控制路径处理。验收覆盖按键后继续真实语音查询。[客户与电话对端回执](evidence/serve-live-sip-dtmf-20261123/README.md)。

### 使用 Realtime SDK 接听电话

应用核验 `realtime.call.incoming` 通知签名后，取出 `event.data.call_id`，并使用该中继所属客户的 API Key。接听配置直接传给 `accept`。收到空正文 HTTP 200 后，可由独立的 SDK 连接管理会话。

```js
import { OpenAIRealtimeWS } from 'openai/realtime/ws';

await client.realtime.calls.accept(callId, {
  type: 'realtime',
  model: 'qwen3.8-flash-next',
  audio: {
    input: { transcription: { model: 'qwen3-asr-0.6b' } },
    output: { voice: 'vivian' },
  },
});
const rt = new OpenAIRealtimeWS({ callID: callId }, client);
rt.on('input_audio_buffer.dtmf_event_received', ({ event }) => handlePhoneKey(event));
```

旁路收到的首个 `session.created` 含当前会话 ID 和已生效设置。应用可通过同一连接处理函数调用、发送 `function_call_output`、请求语音回答；音频走电话媒体。输出预算包括文本和实际的语音量化码，短版本号的一次实测共用 454 个输出 token。播放的开始和排空事件携带对应的 `response_id`。使用 `client.realtime.calls.hangup(callId)` 挂断。已结束的通话及其他客户的通话对该调用返回 404。[业务记录](evidence/serve-realtime-sip-accept-20261123/README.md)。

在电话播报中发送 `output_audio_buffer.clear` 可停播。设置 `audio.input.turn_detection.type` 为 `server_vad` 并启用 `interrupt_response`，来电者发声时会自动打断默认回答。应用从 `conversation.item.truncated` 获取音频项 ID 和 `audio_end_ms`，可用 `conversation.item.retrieve` 核对截断后的音频；服务移除其转录以免下一轮引用未播完的内容。会话也支持只报告检测事件，设置 `interrupt_response: false` 即可。[手动与实际插话验证](evidence/serve-realtime-call-playout-20261123/README.md)。

## Realtime WebRTC 建连与控制

RTC 客户端先创建音轨和有序数据通道 `oai-events`，收集本地 ICE 候选。服务端应用可用固定版本 JS SDK 提交完整 offer。下面的 `peer` 是已创建并设置本地描述的 RTCPeerConnection，`client` 使用所属客户的凭据。

```js
const response = await client.realtime.calls.create({
  sdp: peer.localDescription.sdp,
  session: {type: 'realtime', instructions: '用中文回答。'},
});
const callId = response.headers.get('location').split('/').at(-1);
await peer.setRemoteDescription({type: 'answer', sdp: await response.text()});
```

只传 `sdp` 时 SDK 使用 `application/sdp`。同时指定 `session` 时 SDK 使用 multipart；两种形式均返回 201、SDP 答复和相对的 `Location`。部署允许浏览器跨来源读取该头时，在原生 CORS 策略的 `exposed_headers` 中加入 `location`。长期 API Key 由服务端持有。

ICE 和 DTLS 验证完成后，数据通道收到 `session.created` 和 `conversation.created`，客户端按 Realtime 事件合同发送更新、工具结果和控制命令。语音经 SRTP 音轨双向传输。同客户服务端可使用 `OpenAIRealtimeWS({callID: callId, options}, client)` 接入当前会话。主动挂断使用 `await client.realtime.calls.hangup(callId)`，媒体进程确认排空后返回空正文 200。不同客户使用同一 call ID 接入或挂断均返回 404。

### 给客户端签发短期凭据

服务端应用以该客户的长期 Key 调用签发接口，将响应中的 `value` 交给客户端。下面的配置建立手动提交的中文转录会话。客户端通过原生 TLS 提交 SDP，或用官方 SDK 以 `intent: 'transcription'` 建立 WSS。

```js
const grant = await client.realtime.clientSecrets.create({
  expires_after: { anchor: 'created_at', seconds: 60 },
  session: {
    type: 'transcription',
    audio: { input: { transcription: { model: 'qwen3-asr-0.6b', language: 'zh' }, turn_detection: null } }
  }
});
```

`expires_at` 为新连接的最晚使用时间（Unix 秒）。同一客户的长期和短期凭据共享业务准入。需要文本或语音对话时使用 `type: 'realtime'`；WebRTC 的显式 `session` 可覆盖该对话的配置。模型进程保存签发记录，重启后应用须重新签发凭据。已通过本机原生 TLS 实测的范围见 [WSS 及到期行为](evidence/serve-realtime-client-issuer-20261123/README.md) 和 [WebRTC 音轨与配置覆盖](evidence/serve-realtime-client-rtc-20261123/README.md)。

### 托管部署的中继配置

在 `q4t service prepare` 及轻量托管入口中，`--live-webrtc-address IPV4` 选择本机媒体地址。`--sip-trunks-file FILE` 选择归部署账号所有的私有常规文件，封包机与服务机须能访问同一绝对路径。文件包含中继授权和可选 webhook 凭据，包内只记录规范路径和 SHA-256。需要轮换时用新文件准备后继包并显式部署。自动回退要求地址、路径和摘要一致。实际服务打开文件后复核同一批字节，文件缺失、权限不合格或内容变化会增加 `/v1/config` 的 `sip.activation_failures`，Chat 可继续服务。运行 `q4t live-transport-check IPV4 [FILE]` 可离线复核地址和中继配置格式。完整信令还依赖媒体程序、证书和授权的客户端。
