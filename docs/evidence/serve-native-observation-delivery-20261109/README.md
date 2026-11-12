# 原生观测与受保护留存可回退默认交付

2026-11-09，R04/N06实际v6包CLI/export/retention闭环及默认交付通过。
包b7c25107，服务发布29125ad9；引擎1ff729a0/元数据f44634f5完全未改。
365份引擎来源与原生ELF/来源未改，之前真实模型/进度/缓存业务接受
复用；本轮0模型/0Chat/0设备探针，不扩完整性能或模型质量测试。

真实随包init/observe/metrics、GET/HEAD exporter与独立worker来源
通过，停服事实来自持久STOPPED账本，未知资源不填零。导出子进程
确认回收、端口关闭、观察锁释放。旧v2 CPU合成run经真实CLI迁移、
完整外部归档、计划、删除、幂等resume；0生产run删除，ledger不变。
另3个v6清单/旧v1—v5合同CPU通过。此前原生观察、留存/epoch/历史
依赖的针对证据按来源复用，不重复全套。

保留全部失败：首封装脚本误将包外acceptance harness列为随包来源；
首次CLI脚本期待错误指标前缀，实际正确为q4t_service_business_ready；
第二次CPU驱动使用内核未提供的task/children，改由/proc/stat PPid
核对实际自有worker。仅脚本修复，生产源码与已封包身份保持。

安装在三把拥有锁及持久STOPPED边界完成，仅替换服务入口/两份服务
记录；密钥、配额、service_id、引擎与ledger2216b9dc保持。安装后
help/observe/metrics通过，未自动启动。完整旧v5包保留，回退回执
f5c49225记录原两份文件及不变引擎摘要；按回执恢复服务文件与软链接。

归档只包含报告、摘要、非秘密元数据；私有配置/密钥/套接字不提交。
run限额不等于整个磁盘上限，归档总治理继续R08。R04/N06交付不代表
总目标完成：接着N04密钥/配额与其余全部Serve计划，main尚待Q阶段。
