# 大应用内容透明存储与读取清理故障隔离

2026-11-10。13新增、17直接受影响CPU共30项全部通过，22.493秒；
0GPU、0网络。独立host/实际引擎构建100.102/78.359秒，零警告。
8项来源复核、463来源锚点与5项漂移检查通过；协议产物一致。

ObjectStore已覆盖Chat、Response、Conversation、ConversationItem、
Prompt和Batch种类的写、读、原子批及列表。仅私有持久表示分块；上层
取得精确原内容，根manifest不再交给业务代码。更新以原revision读取
复用信息，按内容SHA/长度复用，并对根做CAS保留；会话删除/移动成员
不会按旧位置恢复已删除内容，原null保持null。批内同内容只分块一次。
列表按展开后的累计逻辑预算分页，修正has_more/游标；单批展开总量
也有上限。此逻辑字节约束不冒称进程RSS硬上限。

实际17MiB原字符串已在StoredChats公开messages处理器、Conversation
创建/成员删除/更新/重启/独立item引用、Responses开始/检查点/完成/
重启/previous路径逐字节回读。大数据为明确的存储协议夹具，不是实际
文档解码或模型回答证据；真实PDF/CSV业务仍须统一执行。

真实对象工作进程在单父/批父持久提交后丢回执，调用保留unknown和
暂存租约；恢复读取确认唯一revision=1，无重放/丢内容。真实读租约
清理丢回执后，成功读取保留已校验字节，错误读取保留原不一致错误；
auxiliary_cleanup_failures累计上报/v1/config，不作为模型重启条件。
短绝对hold兼容8小时父请求，只缩短该存储操作期限，不延长任何租约。

候选引擎`32bdd3cf981fe0a8b7cdf6295112e447b8473449677ebff60abe7bbe7c525bf5`；默认Uploads
cdb5ced9/包96b75252保持STOPPED，未改生产记录、未合main。

这组完成的是自动持久读写接入。继续实现全部HTTP/JSON/入口及prompt/
后台容量、快照引用发布时并发一致性保护、实际业务与能读新格式的
回退发行；所有master任务保持连续推进，不以本组通过结束总目标。
