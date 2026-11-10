# FI02 Office/RTF实际解析CPU接受

2026-11-10。26项首组全部通过，0.565秒；解除未使用RTF代码页的
预加载依赖后，3项直接补验全部通过，0.032秒，其余23项按未变实现与
依赖复用。host/实际引擎必要构建均零警告，0模型/0GPU查询，默认
`cdb5ced9`仍STOPPED。最终候选引擎`a8c719c0`、host`edf04c12`，完整
身份与原始输出见本目录。第一轮二进制SHA未在重构建前采集，明确记
null，不伪称所有用例在最终二进制上重跑；最终身份已记录。

实际生产worker在独立进程内预加载系统解码器，再进入原纯内存
seccomp。只在格式实际需要的转换缺失时返回错误；普通文本不加载
文档库，ELF无新增libarchive/libxml2动态链接依赖。CPU上限120秒、
独立wall时限默认10秒可配置至120秒，2GiB地址空间、owned进程清理与
未知退出占槽保持。输出预算/包展开/XML/工作表/嵌套上限进入同一控制
消息，父端复核身份、行数、名称及累计实际输出，失败不发布部分文档。

- Word：段落、表格、被引用故事、RTF/嵌套文档、严格namespace及单一
  兼容分支；被删除/未引用内容、外链与损坏不混入结果。
- PPT：按实际关系引用的幻灯片顺序及备注提取文字。
- XLSX：共享/内联/转义文字、稀疏坐标、布尔/错误/数值、格式及公式
  缓存和共享引用；不计算公式、不执行宏，不伪造图表数据。每表仅保留
  前1000行，同时验证后续原内容并记录原行数。
- ODF：真实文字/幻灯片、重复行列、空白网格、类型/显示与公式缓存。
- RTF：分组、作用域、Unicode回退与代理对、代码页/字体、双字节转义、
  字段结果、表格控制与二进制跳过；畸形/容量/缺代码页有明确错误。

复用系统API并按官方格式独立实现，没有复制参考解码器源码。依据
[Microsoft Word结构](https://learn.microsoft.com/en-us/office/open-xml/word/structure-of-a-wordprocessingml-document)、
[Excel结构](https://learn.microsoft.com/en-us/office/open-xml/spreadsheet/structure-of-a-spreadsheetml-document)、
[转义字符](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-oe376/bd0aa042-434a-4ca7-b25f-4e1fd25a954d)、
[兼容标记](https://learn.microsoft.com/en-us/office/open-xml/general/introduction-to-markup-compatibility)、
[ODF 1.2](https://docs.oasis-open.org/office/v1.2/os/OpenDocument-v1.2-os-part1.html)
及[官方RTF 1.9.1](https://officeprotocoldocs-f5hpbjgea6b8gneq.b02.azurefd.net/files/Archive_References/%5BMSFT-RTF%5D.pdf)。
RTF参考原件SHA256为`ddb1aa7edfd8e17ed2c49696f9ab19f8d20027cbd7d31753c9a0c8d0a68d02f7`，只保留本地参考，不提交该文档正文。

本次只接受所列解析组件合同。夹具是本项目原始最小文件，不冒称外部
Office应用互操作认证。Legacy Office、Apple/Google、嵌入Legacy/PDF
仍须接入；FI03—FI05完整API消费、持久快照、PDF模型视觉、表格实际
摘要/用量、真实业务和可回退交付全部继续，不能据此结束A09.5或总目标。
