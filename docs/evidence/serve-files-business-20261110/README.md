# Files实际业务接受

2026-11-10。累计2模型启动、3实际生成/模型步、14 Files操作、99.108秒，97次NVML全OK；两次模型正常退出0，独立核对自有进程、端口及GPU锁释放。

官方SDK通过实际核心和原生TLS完成原文/二进制上传下载、分页、跨客户端404及删除。真实项目MODEL.md经Files下载后成为实际函数调用结果，SSE工具参数和后续结构化JSON标题/引用闭环通过。暂停自有IO worker后，File GET返回精确504/object_timeout，readiness仍200，同一模型PID的stateless SSE正确返回42；随后原文件字节恢复并删除。逐进程RPM/TPM、输入输出token和实际模型步与3次生成完全对应，Files未产生额外生成额度。

首轮整体FAILED：驱动误期待503，实际是既有504超时合同。修驱动并做1项CPU后，只补余下一次生成，保留首轮已过项。续验整体FAILED：最后空列表原wire的first_id/last_id为空字符串，被本项目错误的nullable非空字符串schema拒绝。官方Files HTTP为string，声明按此修正并对两轮8个实际File/List/Delete输出及负例执行1项CPU，全部通过；同时保留SDK输出purpose的专用值。生产引擎/入口/运行时完全不改，原失败文件不覆写，不再追加GPU。最终接受由本目录decision及declaration-review、实际wire/metrics/log共同支撑。

本目录保存第二次原件；第一次与故障判定CPU见../serve-files-followup-20261110。三次生成在原log可逐项核对；恢复/删除后的终端账本由真实最终生成快照与无额外模型步日志核对。默认A08未改，私有对象/证书保留在专属工作目录，未公开凭据。

原生入口父准入是绑定真实模型身份的测试适配，未宣称重验Guardian恢复。Files存储/传输接受不等于file_id原生输入消费、Uploads、Batch或完整OpenAI兼容；这些仍须继续。下一步v11可回退发行、所有A09子项和全部总任务。非性能业务，无全模型质量/性能/长稳结论。
