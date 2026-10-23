# 数字格式 suffix 有界验收证据

最终决定见 `decision.json`：VALID、测量完整、统计限描述性、
NO_GO_PROTECTION_REGRESSION。`report-tables.md` 与 `summary.json` 是
分析输出，内部 `REQUIRES_REVIEW` 表示生成数据时尚未作工程决定；
不覆盖后续主审决定。

`report-tables.md` 的入库副本仅去除末尾多余空行；decision 与封存回执
中的报告 SHA 仍绑定原封存文件。其他复制内容保持原字节，入库副本
均由本目录 files.json 校验。

- `report-tables.md`：两个启动块、全部五档及新旧场景、TTFT、普通
  decode、请求原值/首请求、工作量、匹配接受分布、计时与统一RAM。
- `summary.json`：从完整分析提取的指标、请求原值、身份及复用记录，
  去除重复嵌入的成对原值数组；完整分析不被此提取替代。
- `audit-evidence.json`、`audit-costs-summary.*`：独立身份/环境/预算与
  成本/取消恢复审计；`*.log` 为首HTTP门之后执行的直接合同原日志。
- `frozen-plan.json`、`build-identity.json`、`audit-runtime-static.json`、
  `reuse.json`：事前计划、实际构建、依赖身份和证据复用边界。
- `scripts/`：实际离线分析/审计/封存脚本快照，含原工作区绝对路径及
  已封存旧分析 helper 依赖；这些是证据快照，不会自动启动新实验。
- `archive-no-go-receipt.json`：2908条目独立副本封存回执；`files.json`
  校验本目录复制来的证据内容（不包含此README或files.json自身）。

完整本机证据位于
`.q4t-work/suffix-numeric-gate-20261023/`，原始run/lease、全部响应与
SQLite、完整analysis/audit、候选程序及构建源码均在原路径和
`archive-no-go/`中保留。封存不复制模型权重。实际受测源码为82ce03a；
后续结果文档改变是收尾，不是重新构建或运行。两head的原状态oracle
复用限受控两层及实际draft/head边界，不证明任意输入整模型等价。

结果正文见[阶段报告](../../MTP_SUFFIX_NUMERIC_GATE_2026-10-23.md)。
当前默认程序和发布元数据未替换，没有使用接受后的健康请求预算。
