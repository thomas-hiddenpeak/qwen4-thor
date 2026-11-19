# 语义VAD与恢复交付合入主线

2026-11-19：[PR #51](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/51)
已合并，固定head `c388fb254d1fc5887417f080472b776a28fc3aa4`，
main合并点 `53808bf5211058515ff20999eadac7ba95d3d4ea`。

两组公开host-contracts CI均通过。直接脚本导入失败已定向修复；
该修复首次CI重跑遇到Ubuntu依赖下载超时，保留原日志，单次重试后
通过。复用未改变的模型/恢复/交付证据，0新增GPU。

[合并前检查](before.json)、[实际合并](after.json)及[命令结果](merge-result.json)
可复核。默认rev346 STOPPED不变。本地Realtime对话基础、主请求桥接
与新会话工作未进入该PR；分支仍有独有代码，保留当前工作入口。
全部Serve master继续，听感待验与原exact-code/声纹失败保持。
