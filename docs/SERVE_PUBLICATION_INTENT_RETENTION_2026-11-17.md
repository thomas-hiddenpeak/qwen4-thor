# 发布意图的可恢复保留管理

客户端交付在rev256后回退失败，实际在线意图达到128条上限。
当前服务仍STOPPED，失败请求尚未写入意图；客户端CPU通过不等于
此次回退交付通过。旧版本及generation已有归档，意图缺少对应入口。

新增自有Python维护工具，不新增外部依赖或改动模型/控制器。一次
显式选择1—4个旧请求UUID，在publication、deployment、Guardian和
共享GPU锁下要求完整STOPPED。当前16条历史、当前请求及尚未写入
账本的待恢复请求受保护；存在部分意图或活跃文件引用时拒绝移动。

计划绑定精确发布状态、服务、文件inode/hash和维护工具来源；全部
选中项校验后以同文件系统rename和fsync归档。中断可用原计划续做，
不改发布账本或任何恢复预算。恢复先检查容量、碰撞和原始字节。
归档请求在线resume不可用，需先恢复；旧请求按旧revision重试仍被
前驱检查拒绝，不会重新执行发布。没有删除文件、增大128上限或
自动拉起服务。归档目录属于保留证据，不随分支清理删除。

```bash
python3 -B -m tools.serve_recovery.publication_intent_archive prepare \
  --state build/service-native-state \
  --output .q4t-work/owned-stage/intent-archive \
  --request-id <显式选中的旧请求UUID>
python3 -B -m tools.serve_recovery.publication_intent_archive apply \
  --plan <prepare返回的绝对plan路径> --sha256 <prepare返回的散列>
# 需要恢复时使用相同plan和散列，将apply改为restore。
```

prepare的父目录须为本用户所有、组/其他用户不可写的项目工作目录。
发布准备要为本组全部publish/rollback/forward预留意图槽位，同时
检查generation和版本登记容量；不以一次publish成功当完整交付。

8项CPU验证覆盖实际容量拒绝、归档/恢复/新发布、预算保留、待恢复
请求保护、篡改/碰撞、rename后中断、锁竞争、活动引用、过期计划及
恢复容量。只使用明确CPU假模型包。实际维护最多归档4条，随后续
原失败组的9项剩余检查，累计13项成功及1次原失败，0模型动作。
