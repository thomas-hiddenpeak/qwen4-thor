# Chat原生审核默认交付 rev204

实际服务包`4981551e1e9dc104b9a9833f6b6cd8bd5a059c857cefd6b635e8d75f4a60ecd3`，generation
`91704d0be13a99482ab8af8511fac78f0ff7f04f8488c459bd755608652045a2`。18项STOPPED发布、整根回退和再前进
完成，229.152秒，0新增GPU，最终STOPPED。新引擎与双客户端
一起交付，原生入口和恢复不要求nginx/systemd。

Chat标准moderation可调用实际本地分类，input/output独立score/block，
全choice审核，批准后发布block流和存储，辅助模型消耗进入同一配额。
原失败输入经修复后实际content_filter且原回答usage为0；原失败保留。
本地13类分数是当前模型估计，不冒充云端专用模型校准。

核心/client/outer状态和预算字节保持；profile只增加已验能力/身份，
原history/pending/配置/clock保持。整根切换回填原周期，仅递增revision，
未返还额度。人工回退根`c9a8da995471`保留outer和周期迁移；
自动payload`4c95506c74b2`使用已验旧控制及当前新引擎，
不是独立旧引擎回退。原rev196包和状态备份另行保留。

退休generation/版本登记各两份通过可恢复归档腾位，0删除。真实
业务修复组4启动/17请求/238.768秒，含原失败累计8/32/443.027秒，
[业务及原失败证据](../serve-chat-moderation-business-20261117/README.md)保持。
当前控制/原生helper复用精确证据；未声称重复完整恢复矩阵、性能或
长稳。继续PR37实际检查/合main和所有剩余master任务，24小时由用户执行。
