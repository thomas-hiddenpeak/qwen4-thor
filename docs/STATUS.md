# 当前状态

更新：2026-10-17。代码是实现事实来源，验收以[EVALUATION.md](EVALUATION.md)为准。

## 当前交付

工具与验收规则主线收敛：完整历史已远端归档并独立恢复核对，成组裁剪
完成，[PR #5](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/5)已创建，尚未合并main。见[阶段与证据](MAIN_CONVERGENCE_2026-10-17.md)。
保留GPU互斥/归属修复、实际输入trace修正、简化持平通道和有界诊断规则；
runner恢复main，专项probe与大批调查材料由[归档索引](ARCHIVED_RESEARCH.md)恢复。
此次不运行测试、不采样、不新增分支，不能称为新性能或数值验收。

## 已接受生产基线

main 28a3783；src/include/根CMake与本分支一致，build/q4t保持
be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072。
默认MTP k=3，--no-mtp回退普通decode；PLE上传修复与N=4等合同保留。
[数值收口与五档证据](MTP_NUMERICAL_CLOSURE_2026-10-16.md)继续有效，
身份/依赖不变的已通过项复用。当前不是任意模型/任意输入正确性的证明。

HC up、T4 attention和GDN Record/Fold候选均未接受；HC TTFT根因未明，
原因果目标blocked未完成，不自动恢复诊断或把差异称为噪声。微小变化
判定仍受测量分辨能力限制，各档分辨带未建立，不任意填百分比。
旧参考研究计划仅作归档素材，不是自动执行队列。

## 分支与证据

本地/远端仅main及codex/moe-residency-20260930两个分支。研究历史由
三个归档标签保护；20个工作区原路径/HEAD及本地证据保留，清理过的
两个旧工作区修改已按字节归档。默认复用现有入口，合入后评估留存。

历史状态保存在main及研究tag，不作为当前任务安排：
[生产基线历史状态](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/docs/STATUS.md)。
