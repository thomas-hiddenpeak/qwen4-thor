# 原始 Office 解析夹具

`python3 -B tests/fixtures/file_office/generate.py` 确定性生成这里的文件。
内容是本项目自写的最小结构样本，使用Python标准库ZIP/XML，无下载
文档、第三方文档正文或解析器源码；它们用于验证实际字节到内容映射，
不冒称已通过Office应用的完整文件验证或真实模型验收。

Word包括Unicode、表格、删除内容、被引用的页眉/脚注/评论、RTF导入、
严格namespace、兼容Choice/Fallback、损坏和外部/穿越关系。PPT按关系
引用给出与文件名不同的幻灯片顺序和真实备注。XLSX有富文本共享字符串、
转义/字面转义、稀疏坐标、数字格式/缓存公式/共享公式引用、图表页、
1002行以及损坏引用/数值/坐标；条件格式复用编号不能污染实际样式。
ODF有明确空白、被删除内容、演示页、重复行列、稀疏空白网格及公式缓存。

对应用例在 `tests/server_file_office_test.cpp`，全部调用生产独立worker。
RTF逐字节夹具直接保存在 `tests/server_file_rtf_test.cpp`，也使用生产worker。
