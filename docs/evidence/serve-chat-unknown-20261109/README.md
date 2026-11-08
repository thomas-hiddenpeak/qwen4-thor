# Chat真实未知提交缺陷、修复与针对性CPU原件

2026-11-09。第二轮1启动/3条Chat/53.830秒，真实资料工具SSE、读取本项目
文档、回填严格JSON、保存完整工具历史及metadata更新通过。精确暂停自有
存储worker后，生成完成但落盘回执超时，内部commit_unknown没有传播至
公开错误码，实际返回object_timeout。这是实际缺陷，原FAILED与租约
COMMAND_FAILED/child1原样保留；不把通过前缀视为整组通过。

修复统一Chat、Responses及Conversations HTTP出口：内部原因不丢失，
未知写入返回object_commit_unknown，并带原资源ID查询提示；不自动重放。
5/5直接CPU通过，覆盖真实提交后丢回执、JSON/SSE错误、不伪造成功终态、
会话创建原ID查证、已知提交前取消和worker恢复预算。两次构建零警告，
最后一次只有行宽调整；来源290锚点/5负控通过。所有原始记录及索引保留。

只补验纠正后的故障和同PID普通Chat：1启动/2Chat/300秒，累计3启动/
8Chat/600秒。此前通过的3正常生成、2工具生成和相关管理操作按未改依赖
复用。原状态私有复制，不改原件；默认未更换，之后继续全部Serve总计划。
