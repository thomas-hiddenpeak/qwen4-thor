# 冻结 GU 输出的重复存储回收

仅处理 `moe-full-gu-20260924` 的实际输出/重放输出和
`moe-full-gu-fp64-20260924` 的 FP64 舍入输出。模型、reference/、
生产代码及二进制不变。存储内容核对不构成推理验收，不运行 HTTP。

模板复制到 `.q4t-work/prepared/moe-gu-storage-20260924/`，去掉
`.in` 后先运行 prepare.py，再运行 share.py。同名证据目录不能
覆盖；失败后先检查现场及 journal，不能盲目重跑已部分执行的替换。

prepare 按每个专家的三个明确路径生成候选，要求原始 manifest
摘要相同。share 完整重读所有候选，确认普通文件、真实路径、
设备、inode、大小、mtime、权限和单链接状态，全部预检后才开始
修改。原始元数据写入 preflight；每步 journal 都刷新到磁盘。

canonical 去写权限，其他两条路径通过临时硬链接与原子替换共享
其内容，最后核对两个原始 manifest 的所有条目及共享只读关系。
逐路径内容和 manifest 保留；inode、mtime、权限会变化，不能
声称元数据完全未变。释放字节按原文件实际占用块数统计。

冻结共享文件不得 chmod 后原地写入。新实验用新目录，若需恢复
独立存储，按 docs/EVIDENCE_STORAGE.md 制作私有副本再原子替换。
