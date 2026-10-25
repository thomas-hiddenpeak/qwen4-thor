# 报告绑定修复的有界证据

`scope.json` 冻结唯一阶段；`cpu-proof.json` 为32项首suite。
`audit-stage.json` 保留首次完整审计FAIL/INVALID，不改写为通过。
`nvlog-reconciliation.json` 只核其唯一差异：早期日志是最终文件的精确
前缀，目标记录相同；不重跑全审计或放宽warning门。

`offline-warning-review.json` 为先前只读分析原件。其中有关未见NVTX
终止记录的窄匹配文字，由reconciliation勘误：两组均有producer
NVTXFinish，未知的是consumer警告原因。CUDA forced-flush线索不是
当前consumer代码分支证明，304与292之差也不是12个丢失事件。

`memset-workspace-source.json` 是生产形状/workspace来源核对，未执行
新probe，不证明旧6381次缺活动memset的count为零。

完整原件保存在`.q4t-work/suffix-report-binding-20261026/`；最终独立归档
收据另列，不把归档或研究提交视为suffix交付。模型、head和默认程序
未变，两head重复收益按原证据复用。
