# Linear/GDN冻结证据重复内容共享

纯存储治理，无生产/观测器改动，不运行推理测试；不构成正确性
或性能验收。范围严格限制为脚本列出的五个linear/GDN证据目录。

模板复制到prepared新目录并去掉.in，修改输出路径，不覆盖既有
存储审计。本次linear-evidence-storage-20260924。先prepare.py，
仅按原manifest、文件尺寸和nlink=1生成至少16MiB BF16候选；
这个清单不是内容验证。share.py逐候选重新读取SHA及元数据，
全部预检通过后才改为只读硬链接，逐项fsync journal记录替换。
最后重读五个原manifest全部条目，保留原manifest/路径/字节，
文件inode/链接数/mtime/权限变化记录于preflight/journal。

共享文件禁止原地修改或chmod后写入，需在新实验目录建立独立
副本。此清单不能重复用于已共享的目录；重新制作明确范围候选。
模板的旧inode清单不随源码提交，实际候选保存在证据目录。

qsa-capacity-plan.json是后续采集预算，并非已实现/测试的observer。
包括全4096 query的Q/gate/attention、评分、选择、K/V和cache。
实际页表超出预算即停止，不能截断。新observer仍先三侧HTTP，
再运行数值参考；原错题、QSA完整算术与其他覆盖仍未解决。
