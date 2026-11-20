# 追踪验收前置错误保留与修正

第二次命令漏传`--inherit-lease`，第三次驱动在已有三个客户端之外又
新增一个客户端，超过原公平队列三席。两次均在模型启动前退出，
连同首个格式错误累计0模型启动、0生成；不记作模型或业务验收失败。

已显式继承严格租约，并复用既有独立`voice-other`客户端做隔离检查。
原公平队列/模型容量不变，容量核对前移至准备阶段。新增针对性CPU
反例通过，0.001秒；原20项证据复用，运行时身份未改变。

[原第二次失败](sdk-business-02/failure-review.json) ·
[原第三次失败](sdk-business-03/result.json) ·
[清理](sdk-business-03/cleanup.json) · [CPU](cpu-result.json) · [SHA](index.json)
