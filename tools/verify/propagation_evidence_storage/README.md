# 传播证据重复内容共享

仅处理指定冻结来源清单中的数值文件（BF16/F32/F64/I32/bin/SF），
候选至少4MiB。prepare.py只按manifest/stat清点；share.py在任何
替换前重新读取全部候选SHA并核对路径/设备/inode/大小/mtime/
权限/链接数。目标必须是独立inode。已有共享canonical必须已经
只读，不修改其权限；独立canonical可设为只读后共享。

逐操作fsync journal，先建临时硬链接再原子替换目标，目录fsync。
原路径与字节不变，目标inode/mtime/权限/链接数采用canonical，
原元数据保存于preflight/journal。所有选中来源的完整原manifest
最终逐项校验，manifest不改。新数据不得原地写入共享inode。

模板去.in复制到 `.q4t-work/prepared/propagation-evidence-storage-20260924/`，
依次prepare.py、share.py；share stdout留prepared/share.log。
share进程真实终态且全量审计成功后seal.py，stdout留prepared。
封存完整关闭日志，禁止边运行边生成最终清单。新实验使用新目录、
重新制作候选清单，不能复用旧inode记录。

模型/reference目录、生产运行时和其他任务文件不在处理范围。
这是证据存储治理，不运行推理测试，也不构成质量或性能验收。
