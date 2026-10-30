# 审计P2修复主线合入与分支清理证据

2026-10-31，用户明确授权合入main并清理本轮分支。8个提交由
c26e97a快进至2b30496，main已推送；本目录记录合入与清理事实。
运行时接受仍以三阶段Thor证据为准，没有新增模型/GPU/HTTP验证。

| 文件 | 内容 |
|---|---|
| preflight.json | 169项源码、336份修复/77份原审计证据、12封条、默认身份、21工作区及远端核验 |
| rollback-check.json | 最终封条包只读回退检查结果 |
| bundle-verify.json | 合入前增量Git bundle的SHA256、前提提交及完整性检查 |
| open-prs-before.json / open-prs-cleanup.json | 合入前及删除前重新查询的全部打开PR（均为空） |
| merge-result.json | 快进合入与推送命令、返回码及远端main身份 |
| main-ci.json | 交付锚点在main触发的公共host自动检查，全部步骤通过 |
| cleanup-preconditions.json | 精确旧tip、main包含关系、零独有提交、PR/worktree与保护路径检查 |
| remote-delete.json / local-delete.json | 限定旧tip的远端删除及本地安全删除原始命令收据 |
| cleanup-result.json | 清理后的远端refs、保留锚点/研究分支/工作区/证据与bundle |
| post-cleanup.json | 删除后默认身份、源码、4,123条原路径及回退标记复核 |
| files.json | 上述12份原始收据的来源、SHA256与字节数 |

修复分支旧tip及保留的main交付锚点为
`2b304961a743088ccdd0f2f95fc9748671c8e2d8`。只有本地和远端
`codex/audit-p2-fixes`被删除；研究分支
`codex/mtp-prefill-scoring`仍为4076810，21个原工作区保留。
收尾文档提交在该main锚点之上，不改变实际构建身份。

本地额外备份为
`.q4t-work/audit-p2-main-merge-20261031/premerge.bundle`，SHA256为
`34b8551dbd204b041c190a4db7e8f9bf004baeb6ab4b3370303e1fc57923343a`。
此增量bundle要求c26e97a及其祖先已存在；它们保留在main中。
原证据路径列表在同目录`protected-paths.json`，其哈希绑定于
preflight.json，未删除或搬迁原路径。

默认b11ed66c/982c888c保持，实际构建源码98c42bf、工具源码76963ad。
最终程序回退包继续位于
`.q4t-work/audit-p2-20261031/stage3/delivery-package/`，前两阶段
包和失败现场全部保留；不以Git bundle替代程序回退包。
完整验证边界及回退命令见[修复结果](../../AUDIT_P2_RESULT_2026-10-31.md)。
