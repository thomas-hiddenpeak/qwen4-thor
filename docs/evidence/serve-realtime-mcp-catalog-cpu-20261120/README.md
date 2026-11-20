# Realtime MCP真实目录导入与SDK投影：CPU

2026-11-20。12项通过，0.140秒，0跳过：8项投影/边界，
4项实际本机HTTP MCP夹具。原生worker完成初始化、分页tools/list、
请求凭据和过滤，私有目录保留完整schema/会话，公开投影不含凭据或
私有session。另一客户不能读取该目录，未调用远端业务工具或模型。

成功空目录、远端RPC失败及坏schema分别处理。失败持久目录只保留
空可调用集及安全错误分类；Realtime目录item严格符合SDK，失败由
后续会话事件所有者报告，不能混入Responses额外error字段。取消、
过期、坏ID及无权限路由在准入前拒绝且无网络I/O；这不代替后续进行中
取消和异步事件所有权验收。无效投影保留调用方旧输出，公开私有字段
反例拒绝，工具schema中的普通同名用户数据保持。

首次构建缺少FileExpansionOptions完整声明而失败，已保留记录；补
include后22.877秒构建零警告，随后一次成组完成12项检查。没有更改
Responses执行实现或已有合同，定义事务的18项证据继续复用。

默认rev396、runtime和STOPPED保持，0GPU/0LLM请求。网络夹具通过
不等于模型驱动资料查询通过，Realtime公开请求尚未连接本组件。
下一步连接异步目录、审批/调用和响应所有权，完成实际SDK业务及
恢复后可回退交付，所有master任务继续。

[计划](../../SERVE_REALTIME_MCP_2026-11-20.md)、[结果](acceptance.json)、
[索引](index.json)、[原始记录](records.tar.gz)。
