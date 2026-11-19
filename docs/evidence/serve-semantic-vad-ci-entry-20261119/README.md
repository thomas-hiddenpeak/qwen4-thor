# 语义验收直接脚本入口修复（2026-11-19）

PR #51的67cbec7最终head在公开CI完成零警告构建，64组host检查中
63通过、1失败。失败组直接执行test_semantic_acceptance.py时找不到
tools模块；尚未进入13项测试。原失败日志保留，不将其改成通过。

脚本以自身__file__定位仓库根，显式加入导入路径。按CTest相同工作
目录、直接脚本方式、私有TMPDIR和移除PYTHONPATH的环境执行，13项
CPU全部通过，2.316秒/0GPU。生产代码、引擎、包及默认rev346不变，
既有实际模型/恢复/交付证据可复用；后续CI通过后才能合入。

本修复提交只基于已验67cbec7，未带入本地后续Realtime对话基础提交。
该基础尚未接真实模型，不混入当前合入边界。全部master继续，原
exact-code/声纹失败及用户延期听感保持。

见[原件](raw.tar.gz)、[索引](index.json)与[结论](summary.json)。
原CI：[44517542218](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/44517542218)。
