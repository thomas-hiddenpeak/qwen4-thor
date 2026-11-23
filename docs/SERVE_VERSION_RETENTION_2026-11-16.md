# 版本恢复登记的有界留存与可恢复归档

2026-11-16，Chat缓存候选已完成业务、主轻和精确封包恢复；发布前只读
检查发现默认rev166已经保留16个版本恢复周期，达到登记器上限。
尚未执行新默认发布。不能删除旧预算、提高上限掩盖问题，或在切换
默认之后才发现新周期无法安装。

本组为Q02/R06的实际维护缺口，不改模型、控制循环或封包运行时：

- 新增显式离线工具`tools.serve_recovery.version_archive`，在持久STOPPED、
  publication/deployment/Guardian/core/version writer及GPU租约独占下，
  每次仅移动1—4个明确指定的已退休登记文件。
- 当前周期、保留发布历史中的根、未落入journal的发布意图、未完成
  登记和活动状态引用均受保护。归档前后原版本策略、预算、故障、
  Key/配额、profile与outer状态字节保持。
- 计划绑定源代码、原publication/版本状态、文件inode和SHA256；同一
  文件系统原子rename、双目录fsync、原计划重试及受限restore，保留
  原文件和完整前驱快照，0删除，不创建新模型或恢复周期。
- 先冻结8项CPU：真实文件/锁、重复apply/restore、当前/保留根保护、
  pending及引用、变化/碰撞/不安全路径、rename中断、竞争、过期计划
  与restore容量。GPU新增0；原Chat引擎和封包身份未变，复用已验业务。
- CPU通过后，实际默认只归档最多2个明确退休周期，核对原状态不变，
  再继续原已验Chat包的17项STOPPED交付。不在容量问题上停止总目标。

操作入口（路径和周期由实际检查确定）：

```bash
python3 -B -m tools.serve_recovery.version_archive prepare \
  --state build/service-native-state --output .q4t-work/<阶段>/version-archive \
  --cycle <已退休周期UUID>
python3 -B -m tools.serve_recovery.version_archive apply \
  --plan <plan.json绝对路径> --sha256 <计划SHA256>
python3 -B -m tools.serve_recovery.version_archive restore \
  --plan <同一plan.json绝对路径> --sha256 <原计划SHA256>
```

恢复文件需要活动池仍有容量；当前或保留回退周期不允许移出活动池。
这是明确维护操作，不是监控失败自动删历史，也不表示R09整根回退
自监护/周期迁移已经解决；这些继续保留为实际实现任务。

[8项CPU与实际归档](evidence/serve-version-retention-20261116/README.md)通过：
16→14、原政策/default不变，首次夹具setup失败保留；下一步继续原交付。

## 2026-11-24 滚动发布与旧控制器

16 次发布迁移最多涉及 17 个不同根。rev454 的所有 16 份登记均受保护，
其中最早两次迁移往返于同一对根。保留这些迁移时需要为后续发布预留位置。
新控制器保留原目录的 16 个槽位，并允许另存 2 个登记。每个周期的
检查点随登记存入对应目录，归档和恢复沿用原 inode、摘要和预算。
旧控制器仍可读取其原有周期，已满的原目录不会继续增长。

跨这两个目录进行整根回退、再升级或归档时，请用新包附带的服务管理入口。
发布前执行 `maintenance-status` 核对可用槽位和受保护周期；
恢复已归档周期还需在其原目录留有槽位。已消费的回退预算随原周期恢复。
[针对性验证与旧包实际读取记录](evidence/serve-agent-workspaces-20261124/rolling-retention.json)
