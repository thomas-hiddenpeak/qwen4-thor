# 分支清理（2026-10-17）

用户授权适当清理、默认不新增分支，合入main后评估留存。
完整删除前tip/包含锚点见[inventory.json](inventory.json)，均在fetch后核对；
当时无打开PR。删除分支引用不删除工作区、构建产物或证据。

## 范围与留存理由

- 本地删除：codex/mtp-admission-20261007、codex/mtp-foundation-20261008。
  均已被main包含；干净工作区保留原HEAD，改为detached。
- 远端已合入main的6个入口：main-wrapup-20261007、moe-cache-replay-20260928、
  moe-shadow-20260928、mtp-admission-20261007、mtp-foundation-20261008、
  storage-ready-20261007，均带codex/前缀。
- 远端offload的10个中间入口：causality、causality-recovery、decode-log、
  diagnostics、gpu-replay、mechanism、mirror-retention、request-policy、
  supply、supply-observer；完整名称/SHA见清单。全部为保留远端
  codex/offload-mirror-recycle-20261006的祖先，保留研究历史，不称合入main。
- 保留main和当前codex/moe-residency-20260930：主线与未合入工作入口。
- 保留codex/moe-residency-path-c-20261001：tip虽已入main，但本地工作区
  有未提交修改；本地/远端tip不同，分别保护，不合并或覆盖。
- 保留codex/mtp-perf-20261009：5个独有提交及未提交内容。
- 保留codex/offload-mirror-recycle-20261006：23个独有研究提交，并作为
  被清理offload引用的共同保留锚点。NO_GO不作为删除独有历史的理由。

远端删除使用每个ref的预期SHA lease和atomic push；远端有并发更新
则整批拒绝，不强行删除。没有重新运行GPU、HTTP或数值测试。
20个工作区全部保留；不删除ignored文件、不自动stash、不新增分支。

## 恢复与后续

清单中每个删除tip仍可从保留锚点到达；无需依赖reflog或临时备份。
确需恢复时，读取清单SHA，先确认分支名未被重新使用，再执行
`git branch <原本地分支名> <记录SHA>`，或
`git push origin <记录SHA>:refs/heads/<原远端分支名>`。
工作区重新绑定分支须确认HEAD一致及状态安全，不能覆盖其他任务修改。

后续默认复用当前有效分支；每次合入后按独有提交、未提交内容、PR、
证据路径复核留存，不为每个分析或实验新增分支。工作区磁盘清理另行
评估，本次只减少分支入口，不声称释放模型/构建证据占用空间。
