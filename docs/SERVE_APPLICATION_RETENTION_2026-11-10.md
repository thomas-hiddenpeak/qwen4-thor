# A08 持久应用状态留存治理

当前默认5935be0a。原生存储已具备隔离worker、客户端配额、校验和、
原子事务、TTL及恢复扫描；本任务补齐清理决策和运维入口，不替换它们。
以下全部子项必须实现、验证、交付后才能关闭A08。

## 已确认缺口

1. `object_worker.cpp::Prune` 只看 expires_at，过期就删除。
   未结束响应必须先由恢复执行器判断原owner/截止并写入终态，不能
   被存储清理提前删除。时钟跳变和服务长期停机也不能擦掉恢复证据。
2. 会话删除/移除条目保留独立item，这是对象合同的一部分；但永久
   条目脱离引用后没有原生治理入口，可能一直占用客户端持久配额。
   不能改为删除会话就无条件级联清空共享条目。
3. TTL清理没有统一的引用保护决策；必须识别保留对象/活动任务引用
   的状态，同客户端解析，不能跨客户端用同名ID误保留或误删除。
4. 缺少可审阅的清理计划和有身份核验的原生命令，不能让外部脚本
   直接猜测文件名并删除业务状态。

## 实现合同

- **默认自动TTL清理**：在隔离worker中计算待删对象；保护活动任务
  与仍被保留对象引用的依赖。到期的独立对象仍自动归还配额。
  恢复扫描可以看到过期但未结束的响应，确认后持久失败/取消状态。
- **引用关系**：从真实结构化记录提取同owner内的对象引用；保留
  对象是根，沿引用保护依赖。已过期对象构成的无根环可以回收。
  内嵌历史、输出item、previous/conversation/file等引用均纳入，
  用户文本中的字符串不作为对象引用。未知形状/资源不足保守保留，
  报告未完成；不能通过猜测删除。
- **永久对象**：Conversation、Prompt及其正常关联状态继续遵守
  显式删除合同。默认不自动清理永久孤立item；管理员可明确选择
  “未被引用且创建时间早于指定时间”的item清理策略。时间条件与
  孤立时长分开，不伪称已记录脱离引用时刻。
- **原生管理**：`q4t state-retention inspect/collect`，复用现有
  同UID Unix管理连接和隔离对象worker；显式offline模式仅能独占
  已停存储。inspect不执行所建议的清理，collect必须携带inspect
  的计划摘要并复查当前身份/引用；变化返回冲突，不盲目重试。
- **边界与故障**：每次计划/删除有对象数、元数据字节、时间上限；
  每批使用既有事务日志，丢回执报告commit_unknown，重新inspect。
  输出仅含身份/计数/保护原因，不泄漏业务内容或密钥。管理错误不
  重启模型、不关闭健康核心readiness；配额统计以实际文件状态为准。
- **兼容与恢复**：不随发布删除旧数据或改变默认Key/配额/业务账本。
  复用已验后台/密文/压缩历史执行链，检查清理与这些路径的组合。

## 连续执行子目标

1. A08.1 实现有界纯留存规划、引用根/环/owner及活动任务保护，并
   接入自动TTL路径；冻结相应CPU缺陷和边界用例。
2. A08.2 原生inspect/collect、计划身份冲突、原子清理/丢回执和
   live/offline入口；定向CPU验证存储故障隔离及既有管理入口。
3. A08.3 实际SDK创建/历史/会话/后台与原生管理闭环；先准备全部
   驱动再验证，上限2启动/4生成/900秒，不做全质量/性能/长稳。
4. A08.4 可回退默认交付、明确持久状态迁移合同，更新A08完成证据；
   接着A09 Files/Uploads/Batch和剩余A07/A10/A11，不结束总目标。

首次CPU清单在成组实现完成后冻结，不为每个小改动重复运行。
已经验证且未变的数值/工具/原生接入证据直接复用。

## A08.1/A08.2 首次针对性验证冻结

2026-11-10成组实现完成，尚未测试。冻结28项C++（下列），3项驱动
CPU与清单/协议来源一致性；host和实际引擎独立构建，零警告。
不运行全性能或全正确性矩阵。后续实际业务仍为最多2启动、4生成、
4模型步骤、900秒，同一严格租约，已有数值/工具证据复用。

```text
object_retention_extracts_typed_dependencies_not_user_strings
object_retention_active_roots_preserve_chain_with_owner_isolation
object_retention_unrooted_cycles_and_explicit_permanent_cutoff
object_retention_unknown_owner_protection_and_plan_identity
object_retention_reference_capacity_preserves_incomplete_owner
object_retention_expired_active_survives_restart_and_recovery_scan
object_retention_automatic_expiry_preserves_live_same_owner_reference
object_retention_inspect_is_readonly_and_stale_plan_cannot_collect
object_retention_batch_limits_and_multiple_owners_require_new_plan
object_retention_unknown_state_preserves_owner_without_blocking_reads
object_retention_partial_atomic_collection_recovers_without_replay
state_retention_admin_actual_live_cli_large_plan_and_exact_collection
state_retention_admin_actual_offline_ownership_and_no_implicit_cleanup
state_retention_admin_invalid_options_and_envelopes_do_not_start_worker
state_retention_admin_lost_collection_receipt_is_not_replayed
object_store_expiration_reclaims_persistent_quota
object_store_transaction_conflict_or_quota_changes_nothing
object_store_corruption_is_not_missing_or_success
object_store_directory_lock_is_not_stolen
object_store_invalid_journal_is_preserved_and_isolated
state_key_admin_online_rotation_retirement_and_state_continuity
state_key_admin_dependency_recovery_does_not_restart_core_or_channel
state_key_admin_shutdown_cancels_waiting_io_without_worker_growth
responses_recovery_revoked_expired_and_spoofed_scope_never_execute
responses_recovery_resumes_queued_identity_and_fails_started_without_replay
responses_state_previous_history_does_not_inherit_instructions
conversation_state_crud_restart_and_item_retention
chat_capabilities_native_responses_state_and_recovery
```

驱动：`tools/serve_responses/test_retention_acceptance.py`三项，验证
固定请求与预算、清理回执/泄漏拒绝判定、真实已验密文响应判定。
业务：真实SDK会话/历史、原生计划冲突与精确孤立item清理、
后台原SSE/密文、停服独占inspect及重启后继续，账本按每进程核对。

## 首业务投影判定修复（2026-11-10）

首轮2启动/3生成/80.036秒，76次NVML全OK；清理与后台回放实际通过。
GET未include logprobs会按合同省略；驱动误与未投影初始响应全等比较，
现修正为精确投影。生产不变，冻结1项新CPU实际原件判定；仅补余下
密文续聊，最多1启动/1生成/300秒，累计3启动/4生成/900秒。所有
已过业务复用，首FAILED保留，不重复生成或扩大质量/性能矩阵。
