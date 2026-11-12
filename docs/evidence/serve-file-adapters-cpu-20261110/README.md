# FI02统一来源与格式适配组件

2026-11-10。24个不同CPU用例已通过；首23项21过2失败，随后5项
4过1失败，再对唯一失败做回执诊断与1项补验。最终通过由首轮19项
未变依赖、第二轮4项、末轮1项组成。首次编译缺少namespace闭合、
两次inventory更新脚本字段/审查集合错误均保留，修正后门禁通过。
未将脚本错误冒充产品缺陷，也未省略失败原件。

固定上游的Pages、Numbers、Keynote、XLSB通过统一生产分派。Pages
文字、Numbers实际B2/D2坐标和值、XLSB字符串和Keynote原始空页及
原创中文备注有真实内容检查。XLSB首失败暴露合法Default内容类型
声明未处理；现在按Override优先、Default回退分派。Keynote测试
误将动画元数据Transition当可见文字，又误假定页名是1；读取固定
IWA原件和生产回执确认实际为空页page1，修正预期并增加真实XML
备注夹具，未用降低产品要求代替内容提取。MPL来源、具体派生变化
和完整摘要见[夹具说明](../../../tests/fixtures/file_adapters/README.md)。

系统转换回执包含实际codec/product/version/BuildId、目标格式及
转换结果SHA；原文件SHA保持独立。公式值来自转换后文档这一事实
须继续进入FI03上下文和不可变快照，本组尚不声称API集成完成。
原生文字/Office/PDF路径不要求系统转换器，Chat非PDF拒绝发生在
网络/解码执行之前。损坏包不会退到转换器，取消、预算和回收有界。

Google查看链接/快捷方式规范化到导出；原快捷方式、原/重定向URL
及真正取得的字节分别保留，URL只作私有来源记录。实际
[官方Sheets公开样表](https://developers.google.com/workspace/sheets/api/quickstart/python)
通过HTTPS导出XLSX并提取1张表31行、核对表头，3.168秒；导出5654
字节，加快捷方式98字节共5752字节/1个逻辑文件，2个工作进程全部
回收。未打印表内容或带签名的跳转URL，也未使用账户凭据。

第一次实网403是本机DNS将Google映射为198.18.0.153，默认防SSRF
规则正确拒绝。补验只给这次固定公共样表夹具显式配置198.18/15与
HTTPS443；TLS链/主机名验证保持，生产默认网络规则没有修改。
该证据是此环境下的真实浏览器导出，不是Google OAuth/Drive全API
兼容，也不能推断任意私有文档可读。导出格式和授权合同参考
[Google文件导出](https://developers.google.com/workspace/drive/api/guides/manage-downloads)。

必要host/实际引擎构建零警告，最终host575243c0、引擎eafc5627。
最后两次host仅改夹具输出/预期，生产依赖身份与Google已过证据不变。
12个审查来源更新，438个inventory锚点一致，语义映射未被扩大。
0模型启动/0GPU；默认Uploads cdb5ced9保持STOPPED。未跑完整性能
或整模型正确性矩阵，未发行/合main、未关闭A09.5或master总目标。

继续嵌入文档、Chat/Responses所有消费与历史/后台/计数入口、PDF
视觉、表格真实摘要/用量、业务短验和可回退发行，然后全部总任务。
