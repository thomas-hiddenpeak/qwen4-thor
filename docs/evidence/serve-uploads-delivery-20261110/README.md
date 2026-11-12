# A09.4 Uploads可回退交付

8条实际随包/发布命令通过，9.794秒，0新增模型/生成/GPU。当前默认引擎cdb5ced9、元数据d4559e09、v11包96b75252，publication revision36、STOPPED。原Files引擎6a7404c7/包cde078fa保留，原配置、API Key、容量/配额和用量账本逐步哈希保持。

接受链包括[Uploads核心](../serve-uploads-core-cpu-20261110/README.md)、[实际HTTP/TLS SDK](../serve-uploads-http-cpu-20261110/README.md)、[旧包File兼容](../serve-uploads-rollback-cpu-20261110/README.md)、[发现/驱动/早拒绝](../serve-uploads-driver-close-cpu-20261110/README.md)和[真实业务](../serve-uploads-business-20261110/README.md)。真实业务1启动/2生成/20文件操作/53.651秒，52 NVML均OK；全部自有进程/锁清理，原失败记录保留。

Files v11可读已完成文件的唯一块manifest；未知Upload receipts会被旧包保守留存，继续上传操作需新包。回退发布保留代并保留业务账本，不能以回退复原业务时间。默认旧v3容量不扩大；较大Part需显式配置文件限额和存储配额。

Uploads完成不等于A09或总目标完成：接着原生file_id/file_data/file_url文件内容消费、Batch8端点及所有其他A/N/R/Q/G任务。尚未合main；本次不代表全性能/模型质量或长稳通过。
