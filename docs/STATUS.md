# 当前状态

更新：2026-10-17。代码是实现事实来源，验收以[EVALUATION.md](EVALUATION.md)为准。

## 当前交付

**项目描述核对完成（2026-10-17）。** README/简介、CLI展示、架构与
索引已按代码修正，详见[核对记录](DESCRIPTION_AUDIT_2026-10-17.md)。
仅CLI字符串/注释及文档变化，推理控制与数值逻辑未改。q4t零警告构建，
版本/帮助已检查；无模型测试或采样，不宣称新性能验收。

[PR #5](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/5)已于
2026-10-17 09:06:07 UTC按squash合入main，合并提交f3a975c。
最终22文件差异已审查；保留工具/测试身份、生产源码/binary及文档链接
核对通过，最终PR头2046b6a两条既有host-contracts检查均成功。
合并树与审核PR头完全一致；本轮未重复运行任何测试。
见[交付与证据](MAIN_CONVERGENCE_2026-10-17.md)。

当前工作区已切到main，原工作分支本地/远端引用已删除。最终PR头由
archive/main-convergence-delivery-20261017标签保护并从远端独立取回验证；
完整研究与两个旧研究分支仍按[归档索引](ARCHIVED_RESEARCH.md)恢复。

## 已接受生产基线

main包含f3a975c工具/规则交付；推理算法源码与28a3783一致，src/main.cpp
仅展示文字/注释更新。原已接受binary为
be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072，
备份于.q4t-work/description-audit-20261017/q4t-before。
当前build/q4t为描述修正后的构建
8685d8bf4068b81df0f5162d6c55924d93eae793d17664a1b465ca7587d6c6ee，
已检查CLI展示，未重新跑模型验收；旧HTTP结果不记为该新hash的实测。
默认MTP k=3，--no-mtp回退普通decode；PLE上传修复与N=4等合同保留。
[数值收口与五档证据](MTP_NUMERICAL_CLOSURE_2026-10-16.md)继续有效，
身份/依赖不变的已通过项复用。当前不是任意模型/任意输入正确性的证明。

HC up、T4 attention和GDN Record/Fold候选均未接受；HC TTFT根因未明，
原因果目标blocked未完成，不自动恢复诊断或把差异称为噪声。微小变化
判定仍受测量分辨能力限制，各档分辨带未建立，不任意填百分比。
旧参考研究计划仅作归档素材，不是自动执行队列。

## 分支与证据

本地/远端仅main，无新增分支。四个annotated归档标签保护研究历史与
最终PR头；全部20个工作区路径保留，仅当前根工作区随交付切换至main，
其余19个HEAD不变。1248份移出活动Git树的本地证据继续保留原路径。
后续优先复用有效入口，确需独立工作时才建分支，合入后评估留存。

历史状态保存在main及研究tag，不作为当前任务安排：
[生产基线历史状态](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/docs/STATUS.md)。
