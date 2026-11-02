# SRV-01b 接收拒绝与输出失败观测开发合同

2026-11-03，在02b限定交付后的独立业务可靠性包；当前仅冻结开发范围，
尚未实现。本包不扩HTTP端点、模型能力、body额度、超时或重试语义，
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

先成组修改原因传播、HTTP上下文/指标和合同；CPU生产函数/socketpair
覆盖body上限/总量、早期EOF、读取截止、部分头、实际EAGAIN后写截止、
peer关闭、显式取消/请求deadline及heartbeat首次失败；检查计数单位和
资源释放。已有FIFO、模型数值/模板和已证明未变依赖按身份复用。

完成CPU与代码复核后再冻结真实HTTP清单和预算：实际受载body拒绝、
普通输出取消/写失败后的控制可用及恢复，包含真实资料工具回填。HTTP
与模型工作仍全部经同一阶段GPU互斥驱动，记录响应/指标/日志是否一致。
这里尚未批准新的生成/启动次数，不从02b剩余额度或03b失败预算借用。

真实网络访问、鉴权/TLS/网关、持续运行和跨进程任务恢复仍单列；在
部署边界未明确前，验证仅loopback，不从工业化目标推导公开发布。
