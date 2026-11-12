# Conversations服务接入CPU原件

2026-11-09。101份普通原件、114900字节，逐份SHA256见
[index.json](index.json)。源摘要见[source-files.json](source-files.json)。

- 5个不同C++：目录/CLI约束、共享body预算、业务路由、配置脱敏。
- 3个实际原生入口CPU：API Key隔离/查询/方法，容量与实际CIDR，TLS/CORS。
- 10个不同Python：托管目录跨run/controller/model代次、CLI/配额、发行许可
  和旧可选registry、启动FD清理及直接受影响发布行为。
- 配置schema：真实C++序列化、新v2/历史v1、7个负控。
- 3次构建零警告，包含实际q4t引擎与q4t-ingress；未启动模型。

TLS首次漏传--no-tls-ingress、配置schema首次发现媒体v2与旧v1声明
不一致的失败原件均保留。修复后只重验相关项；不以关闭严格校验
通过。CPU入口的后端为传输夹具，真实Conversations处理器/存储SDK
证据在上一目录；不把传输夹具冒充GPU/生产业务。这里的证书与
Key仅为本机CPU测试生成的材料。

未变更默认、未启动GPU业务，不作A07/A08/N07/Q02完成或发布声明。
后续继续Responses实际生成、生命周期与业务短验；此前身份/依赖未变
证据复用。启动锁失败仅用测试临时flock文件，不获取GPU租约。
