# 存储整理审计

报告：[STORAGE_READY_2026-10-07.md](../../STORAGE_READY_2026-10-07.md)。

本目录记录一次性的缓存清理。`cpptools-cleanup-plan.json` 为操作前
精确清单，`cpptools-cleanup-evidence-review.json` 为只读复核，
`cpptools-cleanup-journal.jsonl` 为逐路径操作，`cpptools-cleanup-result.json`
为实际释放量与副作用。`protection-and-storage-audit.json` 复核原目录
七项修改、两个 q4t 二进制及 123 项运行源码身份。

`cleanup_cpptools.py.txt` 是已执行脚本的文本快照，绑定当时的路径、
inode 与隔离目录，仅供审阅，不是可重复执行的通用清理入口。
`archive-manifest.json` 校验封存文件，README 自身由 Git 记录。

其他只读盘点记录解释为何保留冻结数值证据与 reference venv/cache。
潜在重复量不等于已验证或已回收；所有这些候选均未执行清理。
本轮不构成新的推理、性能或物理 RAM 验收。
