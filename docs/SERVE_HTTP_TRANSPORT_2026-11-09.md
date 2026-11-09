# N05 原生 HTTP 接入

本任务是功能/可靠性交付，不作性能优化主张。默认 N04/v8 保持
STOPPED，全部总计划继续。没有把新路由、上传或代理兼容从目标删除。

## 实现次序与合同

1. 共享 reader 支持 HTTP/1.1 chunked、合法扩展/安全 trailer、
   `Expect: 100-continue`，先完成认证、已知长度/容量检查才发临时响应。
   CL/TE 混合、重复歧义、坏 CRLF、截断、溢出、越限不派发。
   持久缓冲保留下一请求，调用者逐请求重新认证/限流；不沿用上次身份。
2. 核心和原生入口实现有界 keep-alive，SSE 使用完整 chunked 帧。
   HTTP/1.0/显式 close 与不可恢复的传输错误关闭连接；顺序处理请求，
   不并行流水、不重试已派发请求、不把上一流续接到新模型代次。
   空闲连接不持有业务/配额租约；头/体、写、空闲及请求数均有上限。
3. 原生可信代理与路径前缀：默认不信任转发头，显式 CIDR 才信任，
   按可信链恢复来源；API Key 始终由原生验证。前置只是可选集成。
   完整路径保留编码/查询，prefix 有边界且不得改变对象/客户端归属。
4. 配置/发行、直接 CPU 夹具、必要 TLS/SDK/真实模型短验、可回退交付。
   HTTP 层保留 multipart 字节；Files/Uploads 业务对象属于 A09，必须
   实际实现，其未完成不能由本组传输验收冒充完成。

共享 reader 初始边界保持头 64 KiB、应用 body 16 MiB、聚合 body
48 MiB；非应用小控制 body 4 KiB。chunked 按解码后字节计客户端及
聚合限额，每片先预留再读入；扩展/尾部合计 64 KiB，单行 8 KiB、
尾部字段最多 128 个。每请求原绝对 header+body 截止不随进度延长。
首批连接策略每连接最多 100 请求、空闲 5 秒；可配置接入随本任务
实现，统一容量 profile 和默认发行事务仍属于 N07。

## 分组验证冻结

先实施共享 reader，一次运行 9 项新增 CPU：逐字节/多种分割chunk及
二进制payload、扩展/尾部隔离、pipeline保留、HTTP版本/Connection、
先认证再100、已知长度/容量拒绝不100、分块限额/租约释放、坏帧/截断/
走私、绝对超时；复用其余模型证据。直接受影响的既有 request、
receive-observation、client-header admission 用例按实际名字列入清单，
编译不算测试。连接/SSE/代理实现前补充该组明确验收清单，再统一运行。
仅具体失败修复后重验受影响项，不扩成全模型正确性/性能矩阵。

协议依据：[RFC 9112](https://www.rfc-editor.org/rfc/rfc9112.html)
（framing、chunked、持久连接）、[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)
（100-continue）、[RFC 7239](https://www.rfc-editor.org/rfc/rfc7239.html)
（可信转发信息）。独立实现，不复制 nginx 源码、不增加外部服务依赖。
