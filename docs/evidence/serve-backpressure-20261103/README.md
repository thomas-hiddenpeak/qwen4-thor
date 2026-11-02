# SRV-03b未通过验收的完整记录

冻结驱动0ba39744、专用ELF903c4f77。默认5642eb68/6aca6a3e保持。
78份原始记录按字节复制，另有README、source-map、local-identities和根seal；
2份ELF/source.bundle保留原本地路径，不安装专用程序。

本轮1生成/0专门拒绝/1启动，严格输出未在1024 token预算内闭合，
55314.963ms日志记录structured_output_incomplete，未观察到EAGAIN。
驱动等待原170秒观察窗后退出，206.739秒执行，guard COMMAND_FAILED、
无ENV_ABORT；正常清理，原预算不扩大，剩余请求/启动不使用。

第一驱动没有读取并保存错误wire，不能补造其内容或声称HTTP客户端
已验证错误；保留请求、日志、170次内存采样、worker reap与guard记录。
本机没有task/*/children，原采样漏worker，不把缺失当0；日志报告的
worker post-exec VmHWM和含继承峰值的wait4高水位独立记录。

CPU首次8项7通过，构建门选错target的1项失败保留，受影响修复通过。
后续修复驱动在同请求结束后保存失败wire、不再等满观察窗，CPU3项
通过；改proc-stat父子发现并核对start ticks，CPU夹具和实际闲置子进程
2项通过，未加载模型重验。后续修复不能追认原验收通过或补回缺失记录。

06:25:12 CST GPU PID空、共享锁可用、无常驻模型/恢复标记，
MemAvailable116.976GiB。正式边界见[结果](../../SERVE_BACKPRESSURE_RESULT_2026-11-03.md)。
