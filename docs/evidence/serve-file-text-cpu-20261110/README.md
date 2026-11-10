# A09.5 FI02 原生文本与表格提取组件

2026-11-10。10项直接CPU全过、host零警告、0模型/GPU。默认Uploads
cdb5ced9保持STOPPED；仅纯解析组件接受，隔离worker、PDF/Office/RTF、
表格真实模型摘要、完整API/持久性/业务和总目标继续。

UTF8保留Unicode/代码原文与组合码点；支持显式BOM的UTF16LE/BE及
UTF32LE/BE，错误编码/孤立surrogate拒绝，不以替换字符改变源文件。
CSV实际处理引号、重复引号、跨行及空字段，TSV/IIF保留实际记录。
前1000行留存并统计原总行数，后续行损坏仍拒绝；不会生成伪摘要或
假定首行是业务表头。CSV规则参考[RFC4180](https://www.rfc-editor.org/rfc/rfc4180.html)，
兼容LF/CR行尾和不等长记录以保留真实资料。

提取文字、单元格、列数/总单元格及取消/截止分别有界；超出不静默
截断。PDF/ZIP/OLE/RTF magic及已知元数据分派独立解析，不能绕过
专用解析器把二进制当文本。本组未调用复杂文档组件或真实模型。

计划/精确用例清单、必要build、原始stdout/stderr、源码SHA及decision
均在本目录，可复核。没有重跑此前来源/获取/TLS矩阵。
