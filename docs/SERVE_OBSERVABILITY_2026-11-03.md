# SRV-01b 接收拒绝与输出失败观测开发合同

2026-11-03，在02b限定交付后的独立业务可靠性包；候选实现已成组完成，
CPU合同已通过、真实HTTP尚待验。本包不扩HTTP端点、模型能力、body额度、超时或重试语义，
不作性能优化，不重启03b严格长payload失败实验。

## 已确认缺口

- `http_request.cpp`的聚合body预留失败目前只设503，未给独立稳定
  error_code；多个读取失败共用bool，早期EOF、读取截止和资源拒绝
  的原因没有统一观测。单body上限与48MiB聚合预留不是实际RAM计量。
- `chat_http.cpp`在进入HandleChat前即可拒绝连接、读取或准入，
  这些不进入chat的requests_total/error统计。不能把已有chat计数
  直接当全部HTTP入口失败总数。
- `http_response.cpp`的WriteAllImpl和context只保留bool/write_failed，
  写截止、请求取消/截止、peer断连、socket失败和heartbeat失败没有
  可供统一日志/指标读取的首次失败分类。重复后续写不能重复计同一次
  连接失败，也不能重新发终态。

已有请求身份、diagnostics、slot等待/response耗时、固定取消原因、
body字节和02b两类准入拒绝计数继续保留；本批不重复替换这些合同。

## 计划实现及边界

1. 为接收失败提供固定枚举，至少区分malformed/too_large、body容量、
   header/body读取截止、EOF/socket、连接准入/排空。保持当前对外
   HTTP状态行为；为body容量明确稳定error_code及必要param。每个
   HTTP接收失败只记一次，不拿响应是否成功写出作为“没发生拒绝”。
2. 写入保留首次失败事实和固定分类；区分逻辑写截止、请求deadline/
   显式取消、peer/socket、heartbeat路径。保留既有截止优先级、部分
   写/头状态及单终态语义。hook及heartbeat不能产生悬空context引用，
   清理先join再发布观测；后续短路写不增加一次新失败。
3. 使用固定低基数计数及已有trace关联。指标只接受预定义标签，不把
   请求ID、任意path、body/prompt、工具参数或原始errno文本作为标签。
   明确计数单位是接收失败还是连接首次写失败，避免与chat结果相加。
4. 分别记录解析/读体、预处理、slot等待、GPU完成和完整本地写的已有
   范围；本批先修实际缺口，不通过改指标名字伪造尚未测量的延迟。
   不把本地write成功当客户端收到了完整业务结果。

## 验证顺序

候选映射：`HttpReceiveFailure`固定18种非none原因（含路由/媒体拒绝），
`HttpWriteFailure`固定8种非none原因，来源handler/heartbeat二选一。
计数分别为`q4t_http_receive_failures_total{reason}`和
`q4t_http_write_failures_total{origin,reason}`；context退出先join再发布。
`/v1/capabilities` observability.version=3声明上述边界；业务校验异常
不计入接收失败，但任何响应的实际写失败均在输出观测范围。

日志以既有trace关联，system_error只保留实际系统调用errno；poll失败
另存固定数值poll_events，不伪造errno。transferred是失败逻辑写中已送
入本地socket的字节，不是连接累计或客户端已收字节。关闭/取消与写
截止的先后仍沿用原控制检查顺序；首次失败后短路写不重计。

先成组修改原因传播、HTTP上下文/指标和合同；CPU生产函数/socketpair
覆盖body上限/总量、早期EOF、读取截止、部分头、实际EAGAIN后写截止、
peer关闭、显式取消/请求deadline及heartbeat首次失败；检查计数单位和
资源释放。已有FIFO、模型数值/模板和已证明未变依赖按身份复用。

完成CPU与代码复核后再冻结真实HTTP清单和预算：实际受载body拒绝、
普通输出取消/写失败后的控制可用及恢复，包含真实资料工具回填。HTTP
与模型工作仍全部经同一阶段GPU互斥驱动，记录响应/指标/日志是否一致。
具体清单和上限在下节冻结，不从02b剩余额度或03b失败预算借用。

真实网络访问、鉴权/TLS/网关、持续运行和跨进程任务恢复仍单列；在
部署边界未明确前，验证仅loopback，不从工业化目标推导公开发布。

## CPU结果与真实HTTP冻结计划

生产ed672a65、同源专用4da3c5d0，均零警告；专用仅FAULT宏不同。
OFF214/ON223、必需180、原生写异常2及grammar后端9通过；驱动10项、
失败取证修订受影响1项和计划输入7项native通过。必需门初次原程序
180全部通过，但stderr插入stdout的PASS行导致工具误判；原记录保留，
检查器分流后同二进制180定向重验通过。其余已通过项复用。

计划8次生成尝试、16个接收拒绝/业务边界用例、2次模型启动。
硬上限10生成/20拒绝/3启动，从首次获得GPU锁开始1800秒包含安装收尾；
备用只给具体失败修复，不自动重试、扩样或重置计时。16个原生生命周期
测试（含实际HTTP监听但无模型）也在同一阶段锁内执行；其中两项CPU
写异常与六项等待合同已经通过的证据不当新模型生成。

1. 生产候选完整head、默认MTP k3、单槽8192/prefill2048，inflight8/
   queued2，loopback18103。三个仅发送头的16MiB请求占满48MiB预留；
   chat/preflight各拒绝一个新body，必须503/body_capacity_exhausted/
   param=body，不释放其他owner的额度。控制端点保持可用；随后三个
   半关闭连接收到body_eof错误，全部预留释放。这里没有实际分配48MiB
   payload，不把预留值当RSS或整机内存峰值。
2. 同次启动分别检查malformed、媒体、路由、method、query、GET body、
   body超限、header EOF；并行制造header/body读取截止，保持原30秒。
   再以合法HTTP但messages为空触发业务拒绝，确认不增加receive计数。
   加前组共16用例，15个receive观测；模型token计数不变、响应code/
   trace/原因与日志、固定标签metrics一致。
3. 合法strict SSE长于瞬时响应的请求占槽后，客户端主动TCP reset，
   保存已收前缀为未知/不完整结果；验证peer_closed首次写失败、chat
   client_disconnected结果、槽/body/strict/worker归零。随后普通JSON
   准确回复710003，再通过固定Node24.18.0/openai7.47.0完成真实
   read_project_record读取docs/MODEL.md 1..24行、调用ID回填与准确
   architectures/引用。该生产组4生成，含1次预期断连，不能记4成功。
4. 同源专用plain、同容量与准入，接收socket仅缩小SO_SNDBUF；普通
   增量SSE逐行列整数，客户端在connect前缩小SO_RCVBUF且不读取。
   首次实际EAGAIN处暂停最多10秒，控制健康/取消202后释放；第一写
   失败应是handler/explicit_cancel，后续短路写不重计，不追加成功
   finish/usage/[DONE]，不能成为完整assistant轮次。
5. 同次专用启动再发独立普通增量请求，无暂停/取消；客户端仍不读，
   等原30秒逻辑写截止，观测handler/write_deadline一次后排空，再收
   前缀验证没有成功终态。最多75秒等待首次写计数。最后普通JSON准确
   回答710003、短strict JSON {"ok":true}恢复。此组4生成，含2个预期
   不完整流。它验证普通增量输出，不重启03b严格长payload方案。

正常完整wire与拒绝封套验证schema；预期失败流保留完整已收原字节和
不完整标志，不伪装成成功响应。即使断言失败也保存原socket前缀，
每socket最多1秒取证，不重发模型请求；统一日志/metrics对账只数首次
原因。CPU覆盖的heartbeat/部分头异常等不能伪称GPU HTTP全排列通过。
parent/worker RSS、MemAvailable分别采样，不相加当整机峰值。

通过后保留旧c16caf05/be4cd021及完整回退链，只安装生产候选与对应
发布元数据；安装CLI/隔离worker握手，同SHA生产候选HTTP明确复用，
不额外启动安装路径模型。失败保留现场，若已替换按已知pair回退。
不作性能非劣、长稳或公开网络部署声明；长期工业化目标保持active。
