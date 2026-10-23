# 独立草稿head与SHA2启动优化主线合入

2026-10-23，按用户明确授权，两项已验收实现一并合入并推送main。
代码集成采用fast-forward，`ab762aa2`→`b76ee2b8`，保留全部8个阶段
提交及首轮失败历史。当前main包含受测运行时；实际部署程序未重新构建
或替换。本次仅做合入、完整性核验、文档更新与分支整理。

## 接受范围与证据复用

- 独立草稿head固定65536行、BF16、显式启用；完整head仍为默认，
  主模型完整投影、target verify、固定k3及接受/恢复/停止策略保持。
  接受子集的数值合同，不声称任意输入的最终输出无损。
- 原head验收82条HTTP及3条部署健康通过；两模式质量22/22、直接
  合同和五档对照通过。五档MTP decode中位收益7.56%～12.68%，
  新增常驻逻辑内存320.25MiB，主模型完整head继续驻留。
- SHA2保留完整SHA-256校验、能力检测及软件回退。54条HTTP及3条
  部署健康通过，启动中位64.626→39.350秒；两模式质量、五档保护、
  1408个摘要比较及14个非法加载通过。同模式输出和接受分布一致。
  decode观察差值约-0.26%～+0.17%，不宣称统计非劣。
- 本次冻结预算为0构建、0模型启动、0HTTP、0GPU测试；实际均为0。
  `9a902a8`到合入tip的`src/`、`include/`、`third_party/`及
  `CMakeLists.txt`无差异，实际binary与sidecar摘要不变，复用原证据。
  原SHA2验收已单列未启用的实验Reg4重编译差异，不扩大复用范围。

详细合同、测量和限制见[head验收](MTP_SUBSET_RESUME_2026-10-23.md)及
[SHA2验收](SHA2_STARTUP_2026-10-23.md)。本次合入不引入新词表规模、
量化、动态k、suffix草稿或新的性能采样。

## 源码与实际部署身份

| 项目 | 固定身份 |
|---|---|
| 合入前main | `ab762aa2cfd7b5f57d34419bc816dcce9607ce6b` |
| 代码集成tip/清理时保留锚点 | `b76ee2b8e0e3ca3a5884f1fbeba88b041ab056a1` |
| 实际binary构建源码 | `9a902a822b96b4ea25ae9f47fa5c0a7862f175c0` |
| `build/q4t` SHA-256 | `f356b2c96330101abf1e5fa3fced495a37f4ea5a270ed4457037e0d55735d313` |
| `build/q4t.release.json` SHA-256 | `422993b0aba5d21462ac3a7e44a78cbb8fb1721b41ca961bd476c679581e7e96` |
| 实际CMakeCache SHA-256 | `9e40007a1decff5ebfdde8b9ee241990e47339717f700d7511417bf950ad4d69` |

发布元数据继续记录真实构建来源9a902a8，不改写成合入或收尾文档提交。
实际cache仍保留在下述SHA2包的`candidate/CMakeCache.txt`。原部署
已完成三模式健康检查且无常驻服务；本次未启动服务或执行部署动作。

## 两级回退包

路径均相对仓库根；包内容、原证据和模型目录未修改。合入前逐文件
核验下述两包的精确清单与摘要，共456文件；SHA2最终封存的1866文件
也全部核验通过。包的`sealed-files.json`摘要见表。

| 保留包 | 文件数 | 回退目标及封存摘要 |
|---|---:|---|
| `.q4t-work/sha2-startup-20261023/delivery-package` | 275 | 75e；`bd4b9c53778728fd776dc7123dc379eff5791b34843a2e686158d952fee16fe6` |
| `.q4t-work/mtp-subset-head-20261023/delivery-package-v2` | 181 | 5a；`0ed28bbf3693366ffaebfef1ce6e78f88de42707cb0ce8aa88ac7451e29cd267` |

完整回退链为`f356b2c9`→`75e85bac`→`5a456351`，分别撤销SHA2及
独立head部署。每一级均保留binary、原release元数据、实际cache和
已知身份校验。SHA2最终封存文件为
`.q4t-work/sha2-startup-20261023/final-seal.json`，SHA-256为
`fe2da9d1c84283ffe04c6958f631ff6eb16e53e0b7fbea22225a02fd1b3ae8e0`。

回退使用保留source中的工具，按[SHA2回退命令](SHA2_STARTUP_2026-10-23.md#交付与停止)
先恢复75e，再按[head回退入口](MTP_SUBSET_RESUME_2026-10-23.md)
恢复5a。每次使用新的独立证据/lease目录并全程持GPU锁；不复用封存
目录。旧75e→5a工具不能直接作用于f356，它会拒绝未知当前身份。
本次没有执行回退或追加回退推理。

## 分支整理与停止

仅清理本地及origin的`codex/mtp-subset-head`。删除前重新读取远端
tip并查询打开PR：两侧main和旧分支均为b76ee2b，打开PR为0，独有
提交为0，source工作区干净。先将source原地detach到b76ee2b，再用
精确`--force-with-lease=refs/heads/codex/mtp-subset-head:b76ee2b8e0e3ca3a5884f1fbeba88b041ab056a1`
删除远端分支，本地使用`git branch -d`。删除后远端旧分支已不存在，
旧tip可由origin/main完整到达；后续收尾提交仅更新文档与凭据。

保留工作区`.q4t-work/mtp-subset-head-20261023/source`及其全部证据，
路径绑定的回退工具、原始日志和构建资料继续有效。其他工作区未动，
没有执行worktree删除、prune或git clean。分支删除不等于删除工作区。

合入预检、预算和封存核验见[preflight.json](evidence/mtp-subset-main-merge-20261023/preflight.json)，
删除前保护条件见[cleanup-preflight.json](evidence/mtp-subset-main-merge-20261023/cleanup-preflight.json)，
实际删除结果见[cleanup.json](evidence/mtp-subset-main-merge-20261023/cleanup.json)，
合入后身份复核见[postmerge.json](evidence/mtp-subset-main-merge-20261023/postmerge.json)。
本地收尾凭据目录为`.q4t-work/mtp-sha2-main-merge-20261023`。
本轮结束，不追加候选或采样。
