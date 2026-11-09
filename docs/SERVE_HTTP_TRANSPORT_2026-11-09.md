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

### 连接复用/SSE 组（实现前冻结）

共享 reader 28 项已通过并归档。下一组仅验证新响应 framing 与实际
入口受影响行为：JSON/错误后复用、SSE chunk/心跳/终态、异常或截断
不补成功结束、HTTP/1.0 回退、每请求独立身份/请求ID/配额、顺序
pipeline、客户端断开、模型换代只影响后续请求、100及分块请求、
空闲/请求数边界。用实际 q4t-ingress + CPU 后端夹具和共享响应
helpers；模型真实执行留到 N05 组合短验，不重复模型数值/性能矩阵。
现有依赖这些代码的响应/观察及入口用例选择直接受影响项，执行前
记录精确名字/数量；未改变的 reader 证据复用，若新改动影响它则
只补受影响项。

### 可信代理/前缀组（实现前冻结）

原生policy v4增加独立http配置：keep_alive_requests 1..1000（默认100）、
keep_alive_idle_seconds 1..60（默认5）、request_timeout_seconds 1..120
（默认10）、path_prefix（默认空），proxy默认null。
proxy显式选择`forwarded`或`x-forwarded`，列出trusted_cidrs，并声明
是否允许前置剥离已配置prefix。v1—v3默认行为及只读旧包仍须兼容。

只有直接TCP对端受信任才解释转发信息，按for链从右向左穿过受信任
代理，到首个非受信任来源停止；未受信任前缀不能覆盖来源。API Key
仍由原生验证，私有客户端头一律拒绝。Host按有效公用Host校验；proto
仅接受http/https。采用X-Forwarded系列的代理须覆盖Host/Proto/Prefix
头，不能把客户端自填值原样当授权事实；原生检查字段和配置匹配。
未知/混乱/无法证明来源的已启用代理链拒绝，不回退成受信任代理IP。

prefix只做一次有分段边界的移除，保持剩余原始编码/查询；受信任
proxy声明X-Forwarded-Prefix且等于配置，才可选择接受已剥离路径。
直连与前置两条路径均要通过。CPU验证覆盖双栈可信链/伪造、strict
配置、header解析、prefix与编码/对象归属、实际原生HTTP/TLS直连与
代理、定制超时/请求数；精确清单在组实现完成后、首次执行前冻结。
随后补包合同和完整N05实际业务/可回退交付，不能据此删除A09上传
对象或其他未完成任务。

### 可信代理组验证结果

27项（8C++/19Python与实际入口含TLS/可选CPU前置）全部通过，
构建零警告，原件见[evidence](evidence/serve-http-proxy-20261109/README.md)。
尚未完成发行/旧包兼容与实际模型业务，不宣称N05交付；0GPU。

### 发行兼容与业务验证准备

新发行使用v9，Python文件清单与v8相同，绑定新增framing/proxy源；
v1—v8只读验证保持，policy v4不能标记旧transport发行。新入口
仍接受v2/v3默认策略。STOPPED升级/回退保持当前Key、限额、用量
和核心预算，不自动启动模型；v6的一次性状态格式封锁继续有效。

冻结针对性CPU：6项新包/状态兼容用例与11项直接受影响用例；
另只读核对当前真实v8默认及回退包的摘要/来源，不加载模型。
必要重编核心/原生进程，零警告；运行时业务驱动准备后另冻结
1—2次启动内的实际HTTP/SDK预算，届时才执行GPU租约。
