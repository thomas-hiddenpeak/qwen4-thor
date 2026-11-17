# ASR codec归属连接修复

2026-11-17，service-http-03原FAILED保留：1模型启动/1创建、38.586秒。
真实转录已通过HTTP读包，但处理器传入空BinaryAccess.scope，被
已有codec隔离合同拒绝（503 object_scope_unavailable）。原生资源
清理通过，1606项分配全释放，NVML租约COMMAND_FAILED非ENV_ABORT。

将已有认证客户端映射为client:<id>，本机入口为local:anonymous，
随原deadline/cancel传入codec；不删除隔离检查。改动只连接归属，
原21项codec、34项服务及3项传输CPU行为未变，复用其证据；不为
三行连接代码新增镜像实现的断言。直接验收为下一次真实HTTP业务。

下一组保持1启动/9创建/600秒，具体失败修复后才重做未完成链；
当前未宣称真实转录通过，默认rev266及day不变，所有master继续。
