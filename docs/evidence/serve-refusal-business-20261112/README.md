# A07原生拒绝：真实业务短验

`ACCEPT_NATIVE_REFUSAL_BUSINESS`。候选引擎`18c44ab1f301a0f7f69775f815295e649ecd8bbff8918b2de0a795cfa63cc9f8`，源码`b08f82a1`。
1次模型启动、6次真实生成、1次只读工具执行；驱动59.554秒，
严格租约60.217秒，54项观察/读取。无自动重试、无性能/全质量声明。

模型真实生成Chat拒绝与实际token logprobs；Responses在严格schema
模式选择拒绝分支，产生完整refusal delta/done/item/terminal事件。
官方Node SDK成功消费原始JSON/SSE。存储、原事件回放和后续历史查询
通过，普通17+25 JSON Schema返回42；实际读取docs/MODEL.md前三行，
模型按原工具结果返回正确标题和引用。测试marker只是公开合成数据。

原默认rev72及发布元数据不变；自己的进程、端口已回收，GPU锁释放。
来源/二进制/默认hash前后复核通过；轮询不证明没有短于采样间隔的干扰。
此证据仅覆盖列出的改进和直接保护项，原生可回退发行仍须完成。
其余A07/全部总计划继续，不因该组通过停止。
