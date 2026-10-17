# 已归档研究与恢复入口

主线只维护日常验证工具与接受规则。以下完整研究保存在远端annotated
标签中；引用使用固定commit，不依赖已经删除的分支。归档不改变原有
NO_GO、未决或局部通过的范围，不自动启动下一阶段。

| 标签 | commit | 内容 |
|---|---|---|
| archive/main-convergence-delivery-20261017 | 2046b6afe7b14b49a99f35c84a45f40cefa61e3f | PR #5最终审核头及完整收敛提交历史，squash后删除工作分支的恢复锚点 |
| archive/main-convergence-20261017 | 9a5bc0cfc92e92a8db953092691072fed5e2ede3 | HC/GDN/参考源码调查、专项工具、证据、日志、分支清理与收敛方案 |
| archive/mtp-perf-20261009 | d802fce3ccc4ca8129f94a790c086516713da053 | 旧MTP性能归因与状态研究，非当前数值准入 |
| archive/offload-mirror-recycle-20261006 | 7672b591644add0bbc05b8782da3127c80edc04d | offload完整NO_GO研究链，含此前10个删除入口 |

## 固定历史入口

- [原收敛方案](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/MAIN_CONVERGENCE_2026-10-17.md)
- [逐文件分类清单](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/evidence/main-convergence-20261017/inventory.json)
- [HC up复审及NO_GO](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/HC_UP_REVIEW_2026-10-16.md)
- [TTFT因果调查与未完成项](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/HC_UP_TTFT_CAUSE_2026-10-16.md)
- [GDN Record/Fold NO_GO](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/GDN_RECORD_FOLD_2026-10-16.md)
- [外部参考综合计划（未实施）](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/REFERENCE_INTEGRATION_PLAN_2026-10-16.md)
- [研究分支评估](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/RESEARCH_RETENTION_2026-10-17.md)
- [两份未提交源码/补丁归档](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/evidence/research-retention-20261017/manifest.json)
- [研究标签与清理结果](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/evidence/research-retention-20261017/cleanup-result.json)
- [2026-10-17完整历史日志](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/log/2026-10-17.md)
- [2026-10-16完整历史日志](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/9a5bc0cfc92e92a8db953092691072fed5e2ede3/docs/log/2026-10-16.md)

## 按需恢复，不新增分支

```bash
git fetch origin tag archive/main-convergence-20261017
git show archive/main-convergence-20261017:docs/HC_UP_TTFT_CAUSE_2026-10-16.md
```

若需文件副本，先核对tag剥离后的commit等于上表，再用git show导出至
build/或.q4t-work/独立目录；不要覆盖现有未提交内容。旧未提交夹具
在归档内按文本保存，可按manifest核对SHA后恢复。main只保存恢复索引。

本机全部工作区及原证据路径保留，旧研究worktree保持原HEAD detached。
某些压缩payload只在本地并依赖恢复映射，远端源码标签不包含这些大文件；
不要据Git干净状态删除其目录。后续磁盘清理须另评估，不属本次范围。
