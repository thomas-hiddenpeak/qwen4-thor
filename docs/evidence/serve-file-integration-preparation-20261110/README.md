# 综合文件业务验收准备与兼容回退构建

2026-11-10。驱动4项针对性CPU检查通过；首轮3过1失败为PDF请求误选
纯文本schema，改为现有视觉schema后仅补1项通过。原请求/生产代码
不变；原件保留。用例检查固定PDF原字节、请求/profile、真实答案
判断、准备用量响应头和公开stored-message投影的重建。

前一提交eee091b4634d95f855e8c5937bb0a4a3c500bcda在独立detached
稀疏工作区编译。新工作区最初--no-checkout空index检查失败，确认
仅新建空区后加载其固定HEAD；未覆盖用户内容。首必要构建22.294秒
因服务工具禁止在其所属源码区外生成而失败；保留目录/日志，另在
其自身.q4t-work/engine-build配置，158.477秒零警告完成。固定依赖
复用既有经身份验证源码，无下载、模型加载或主默认改动。

候选引擎43472e764d89139995b2e967ccb7c62d4a768f922ef09bebd02abe082164d082。
回退构建2657eb168192aa6c9cf3e1dd77c52d1f9dea053d237777dc4b56e18d30134a96。
回退源码/产物路径保留于.q4t-work/serve-file-inputs-20261110/rollback-source，
其状态格式支持前期已验的InputSnapshot与ApplicationPayload。它仍有
较低旧输入预算；不能把构建成功宣称为已验回退包，必须实际跨版本
读回。更旧Uploads包继续保存，但不能读取这些新状态格式。

下一实际业务冻结上限3启动/7生成/9模型步骤/1200秒，同一严格GPU租约。
覆盖默认MTP普通Chat、PDF文字与页色/预检tokens、CSV新准备SSE/计数、
冻结历史零重复准备、17MiB大原件跨版本回读和同一状态目录回退生成。
Chat从公开content_parts重建原输入重新处理PDF；冻结快照复用由
Responses previous链路验证，不把两个协议混为一谈。每个进程单独
核对RPM/TPM/实际tokens，显式正常退出/切换；不自动重放请求。

本记录0GPU，尚无综合业务/生产发布/main合并结论；继续全部总目标。
