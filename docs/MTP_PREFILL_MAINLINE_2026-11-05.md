# PR #9评分提取主线合入记录

2026-11-05（Asia/Shanghai）。用户授权“继续推进合并主线”，
[PR #9](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/9)已于12:51:24
合入main，合并提交`cdea60e052a57899bce8081abd2d3bd3753a2a4d`。
后续开发可基于main；本轮仅合入收尾，工业化目标保持暂停。

## 合入身份与检查

| 项目 | 核对结果 |
|---|---|
| 原main | `f2f45526dbbe5acc69c997ad9af2c012b123636a` |
| 已验PR head | `8019a10fa6f1a73d159733f708429463aa713cae` |
| 合并方式 | merge commit，两个父提交分别为上述main/head，保留全部阶段历史 |
| head与合并树 | 均为`ac33f88a2d5580981f53a076efdd4f3ddcf40a25`，全树差异为空 |
| 运行实现 | a4009460；223份运行/构建来源逐一匹配已交付包，其中四个评分运行文件及CMake为本次变更 |
| 客户端隔离 | PR全部49个变更路径与10个未验客户端文件无交集；原文件摘要逐一一致 |

合入前，精确head上的[PR检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/41969428200)
和[push检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/41969419725)
均为SUCCESS；合并后[main检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/41970365965)
也已通过，详情见[机器回执](evidence/mtp-prefill-mainline-20261105/merge.json)。
这些公共host检查不替代Thor实机验收。

原[限定性能与正确性接受](MTP_PREFILL_COMPLETION_RESULT_2026-11-05.md)
及安装健康证据直接复用：198有效HTTP、两head独立BF16评分/代表状态/
缓存合同，以及6个安装健康请求。合入未改变运行源码或已安装文件，
本机没有新增构建、模型、HTTP、GPU测试或部署。17个含负向计时观察
的case和原失败尝试仍保留，接受不升级为统计非劣或旧FP32逐位等价。

## 默认程序与接入边界

本机`build/q4t`仍为`a56bd448ba45e31ba223e73212090a3aea76be9d9a17b06ad0d13c44196544a6`；
`build/q4t.release.json`仍为`914e067febf6d6bab36cafb65548f3f64c3f34c9f0181ba95fb1b7c68752f861`。
协议指纹为`cc321239ced843d6d618d07b72ed81ed65958b2fdc29261fc2b042d9118c40d5`。
发布元数据继续绑定实际验收来源，不因合并或文档提交改写成新构建。

原生Chat可依当前默认；已验托管生命周期仍固定v6包fcff7218及其
61abe09f/ce925d51、15d7ccb1组合。持久任务已验迁移链仍止于15d7ccb1，
没有到a56aed21或cc321239的接受。SDK兼容证据也按原已验版本/身份使用。
详情见[Serve交接清单](SERVE_HANDOFF_2026-11-04.md)，不以评分合入扩大
这些入口的资质；客户端10个文件仍未验收，未纳入本次提交。

精确父对8e9b4ce7/e61bed7c与依赖保留在
`.q4t-work/mtp-prefill-adoption-20261104/delivery-package/previous/`。
回退需按[交付记录](MTP_PREFILL_COMPLETION_RESULT_2026-11-05.md)核对状态和
共享GPU租约；正常交付完成后不能把未完成安装的rollback入口当自动降级。

## 分支与证据路径

本轮清理范围单独限定为`codex/mtp-prefill-adoption`。本地/远端旧tip
均为8019a10f，完整包含于main的cdea60e0；无打开PR，远端删除使用
该精确tip的lease，随后删除本地分支。没有删除工作区或证据目录。

- `codex/mtp-prefill-scoring`仍为40768107，相对main有18个独有提交；
  只提取评分改动不等于研究历史已包含，保留此本地/远端分支。
- 根工作区仍在`codex/main-reaudit-fixes`的f2f45526，10个客户端WIP
  原样保留；未stash、提交或删除。它不是当前main的干净检出。
- `.q4t-work/mtp-prefill-adoption-20261104/source/`保持冻结2a0a92c5；
  原始失败/补验/健康、归档和回退包保持原路径。
- 同目录的`review/`保留，当前交接文档在此整理；交付元数据绑定其
  工具及计划文件摘要，后续不得把它当可随意删除或修改的临时目录。

本机详细回执位于`.q4t-work/mtp-prefill-adoption-20261104/mainline-merge-20261105/`，
包括合入前身份和清理前后快照；上述机器回执提供精简版本。
后续开发使用main独立工作入口，避免改动冻结证据依赖的source/review。
