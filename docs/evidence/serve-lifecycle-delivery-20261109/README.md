# N07 原生生命周期默认交付

2026-11-09，功能/可靠性针对交付。实际默认引擎 `3072f28c`、
引擎元数据 `1b507ad9`，原生 v10 包 `22849b0d`。封装后 8 条 CPU
入口/客户端检查与 12 条真实安装操作通过，实际选择 N05 旧代再前向
恢复 N07；发布 revision 6，STOPPED，安装 0 模型/Chat/GPU 查询。
核心与 Key/限额/用量账本字节保持。默认数值容量沿用 N05；自定义
容量真实业务证据见相邻 serve-lifecycle-business-20261109。

首发布因 build 目录 0775 被保护拦下（1 条失败），尚未创建发布意图；
保留 delivery-01 原件。只把自有 build 目录本身改为 0755 后继续，
不递归更改、不关闭保护、不覆盖首次记录。delivery-02 为实际完成。
release-01/receipt 的 recovery_accepted=false 是封装自身不宣称服务
验收的原始回执；delivery-02/service.release.json 与最终 decision.json
记录已接受交付，prerequisites 决定不冒充安装结果。

旧代 `e77f1dba92ea91e901b5836558debc5e1761ef7e058282023c7a50bdb4a7f7a1`
保存固定程序、两份元数据、服务指针与接受记录副本。后续回退通过
当前合格 q4t service publish-rollback，读取当前 revision 并使用新
request ID；不要恢复旧业务账本，不用旧工具识别新包。服务启动仍是
显式操作。build 四个默认入口由发布事务管理，开发使用独立构建目录。

本目录仅含代码、回执与状态摘要/散列，没有 Key 或私有策略明文。
不声称完整性能、全模型质量、长稳或 main 合并；N06 新诊断缺口及其余
总任务继续。index.json 绑定本目录所有原件（不包含索引自身）。
