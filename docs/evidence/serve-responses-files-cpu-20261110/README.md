# Responses文件准备与持久执行状态CPU验收

2026-11-10。19/19针对性CPU接受，0.865秒，0网络/模型/GPU。
7新用例及12个直接受影响的规划/历史/后台/compaction/moderation用例。
实际引擎必要构建54.076秒、最终host4.128秒通过，零警告；16源码
锚点审查，453总锚点和5漂移保护一致，默认Uploads STOPPED保持。

Responses执行owner现在调用真实文件展开：原始history保留File ID和
原始part，模型计划使用实际提取文字与页面/像素。纯来源/工具/schema
验证先于获取，普通stateless文本/inline文件不依赖ObjectStore启动。
真实PDF与WebP到达同一模板/CPU媒体准备，detail保持。原File删除和
对象存储重启且文档解码器不可用后，previous仍恢复相同来源内容；
外客户端被拒绝，Conversation在store=false时也接管输入快照引用。

准备状态和generation readiness分开。queued在提交前保存原始提取，
恢复必须有完整冻结输入，不能重抓URL。表格步骤完成后创建新不可变
版本并检查点，已开始记录不能按queued重放；generation在所有表格
完成前不可用。CPU表格输出/用量为明确协议回执夹具，真实模型步骤、
完整用量和调度仍须接入并验收，不能据此宣称表格业务通过。
未知快照提交不重试、不删除现场；未知外层发布保留准备hold至到期。

原件保留：首host编译错误为测试遗漏必需error_param参数；修后首CPU
SIGSEGV，1次有界gdb定位为测试传入nullptr。生产调用未变，改为实际
输出字符串后原19项重跑全过；首次缓冲输出无逐项证据，未假称复用。

候选7bbbea35584dbd6eac08d9e0fe48bc535c4f3f524784621cedc0222a9843a6b1，
来源/二进制身份和所有构建/失败/诊断/接受原件在本目录。未发行或合main。
继续Chat/Responses实际表格调度、计数/preflight/真实业务与格式兼容
回退及全部master任务；本组件通过不是文件输入或总目标完成。
