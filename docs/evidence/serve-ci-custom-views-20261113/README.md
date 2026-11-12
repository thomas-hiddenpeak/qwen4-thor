# PR24 custom会话投影夹具修复

PR/push两组CI均57/58任务通过，唯一失败是历史夹具将custom结果
的内部Item直接当作公开ConversationItem校验。固定SDK的两种类型
不同；生产投影已分别处理，夹具仍调用旧缺省视图。

按实际合同保留内部及input_items的completed断言，公开会话则使用
明确的Conversation视图并检查status/created_by不外泄。所有32种
输入仍逐项解析、31种公开会话仍逐项校验，没有删除失败向量或放宽
schema。原始CI失败日志、来源和零警告构建保留。

在精确6effcb3e隔离源码加这一个测试修复上，6项针对性CPU首过，
无skip。生产源码、默认rev102和GPU均未改动。继续真实CI及PR合并，
全部Serve任务保持active。
