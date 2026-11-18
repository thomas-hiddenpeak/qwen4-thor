# 后续原生音色构建空间：历史重复记录无损共享

完整Base与验证产物保留后只余约450MB。核对三组历史run/archive
副本的完整SHA、字节数、权限/归属和mtime均相同；每组先保留独立
gzip并解压全SHA确认，再原子替换第二个路径为同内容hardlink。
六个原路径、字节、mode及mtime保持，未删除历史证据，净回收
1,645,748,224字节，操作16.911秒；0模型/GPU。模型目录、默认与
回退包未动。/proc可见范围没有打开这些inode的进程，不可访问的
PID另记，不能宣称观察覆盖全部外部进程。

独立备份及恢复入口保留在本地
`.q4t-work/serve-custom-voices-20261118/retained-logs-dedup-01/`。
`restore.py`先核对备份SHA，再以原子替换恢复每组独立文件；恢复
要求保留256MiB空闲门。后续编辑历史共享文件应原子替换，不能
原地覆盖。压缩原日志不再复制进Git，原manifest及恢复源可查。

[结果](result.json)、[逐件SHA](index.json)、[记录与恢复脚本](records.tar.gz)。
