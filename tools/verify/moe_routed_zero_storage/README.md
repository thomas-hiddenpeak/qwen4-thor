# 冻结的路由合并前清零证据共享

仅处理 moe-full-down-20260924 的48份 `.combine-before.f32`。
模型、reference/、生产代码不动。存储校验不是推理验收，不跑HTTP。

模板置于 `.q4t-work/prepared/moe-routed-zero-storage-20260924/`，
去掉 `.in` 后先执行 prepare.py，再执行 share.py。已有证据不可
覆盖；失败要先检查 journal 和现场，不盲目重跑部分替换。

prepare 从原始manifest选择固定48条路径，并核对正零缓冲摘要；
share 完整重读全部文件并预检类型/路径/元数据，然后去写权限、
通过临时硬链接原子替换副本。每步journal刷新到磁盘，最终验证
整个原manifest及共享关系。保留原路径/内容，inode/mtime/权限
变化和原始元数据留档；参见 docs/EVIDENCE_STORAGE.md。

本次候选最初由同样的逐层路径/摘要规则生成；prepare模板补充
显式正零摘要断言。实际文件内容由share在变更前全部重读确认。
