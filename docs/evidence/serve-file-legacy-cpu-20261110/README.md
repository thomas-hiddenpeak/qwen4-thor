# FI02旧格式文档转换CPU接受

2026-11-10。本组共14个独立用例，首13项8过5失败；两次有界诊断和
定点修复后，最终9项直接检查全部通过（2.317秒）；收尾审查发现目录租约新增字段
影响两个已过用例的析构依赖，保留同一二进制再补这2项（0.016秒）通过。
另3项按未变依赖复用首轮证据。每轮执行前来源及二进制SHA均保留。最终host
`2425bd1c`、引擎`3b67937c`，全部必要构建零警告。默认`cdb5ced9`
保持STOPPED；0模型/0GPU查询，无完整性能或整模型正确性矩阵。

实际原创DOC/XLS/PPT通过独立生产worker读取：Unicode/段落、真实
sheet名称/行列/数值/公式及幻灯片顺序保持，返回原文件SHA。ODF重复
空行只在后续数据需要时计入行号，不把整片尾部格式网格冒充数据。
原生ODG绘图页文字也有实际夹具。全部源文件与生成命令可核验；这些
最小文档不是全部复杂文档、图表、嵌入对象或Apple格式的认证。

只在该子进程动态加载系统LibreOfficeKit 24.2.7，核心ELF无该库、
libarchive/libxml2的DT_NEEDED。使用官方C ABI，没有复制第三方实现。
依据固定版本[初始化实现](https://github.com/LibreOffice/core/blob/libreoffice-24.2.7.2/desktop/source/lib/init.cxx)、
[Calc配置](https://github.com/LibreOffice/core/blob/libreoffice-24.2.7.2/officecfg/registry/schema/org/openoffice/Office/Calc.xcs)和
[Writer配置](https://github.com/LibreOffice/core/blob/libreoffice-24.2.7.2/officecfg/registry/schema/org/openoffice/Office/Writer.xcs)，
每请求私有profile禁用宏、主动内容、链接更新和强制全表重算。转换器
仍可能处理易变公式；FI03必须携带转换来源，不能把转换后缓存说成
原二进制文件的逐字节原缓存。该集成项保持未完成并继续实现。

默认30秒/2GiB地址空间、50,000,000字节转换结果，累计尝试写入256MiB、
文件/目录创建2048次、线程创建64次；可收紧，硬上限见源码。只有本次
0700私有目录可写，系统库/字体只读；网络、外部进程和GPU设备访问
均不授权。真实检查拒绝越界读取/写入、旁路writev/fallocate、共享
可写映射，并确认线程继承。无隔离执行不是回退方式。

首失败暴露系统注册表依赖只读MAP_SHARED。采用
[seccomp TRAP](https://man7.org/linux/man-pages/man2/seccomp.2.html)
在本子进程将其映射为MAP_PRIVATE；后续mprotect为可写也只产生私有
副本，测试确认原文件未变。直接共享写入仍被拒绝，不放开IO预算。
固定平台为本机AArch64/Linux6.8；x86代码已实现但未冒称异机验证。

第二次诊断证实Word已完整写出、清理显式document/office对象并传输
结果后，在库全局atexit析构中崩溃。一次性worker现执行显式清理和
完整传输后使用_exit，不运行多余全局析构；目录租约保留至内核退出。
父进程仍要求完整结果、EOF和正常零退出，之后另起owned清理worker，
再由原生解析进程验证ODF语义。日志中的status=9也包含既有原生解析
协议在完整回执后的主动清理以及资源/取消故障注入，不能将其误读为
允许旧格式转换崩溃后发布。转换异常/超额从未发布部分结果。

私有根目录有所有权、权限、固定标记与非阻塞租约，拒绝无关数据和
符号链接根。实际128层目录与符号链接清理保持外部文件不变；未回收
继续占槽，下一请求不能积累新的工作目录。取消/期限及失败后的真实
文档继续可用已验。缺失的组件只影响对应请求，不触发模型恢复。

原始失败/构建/诊断结果保留；完整strace在工作目录，路径和SHA见
diagnostic-traces.json。首次inventory核对因更新脚本取错字段未完成，
门禁按预期报来源过期；修正后仅更新13个已审查来源，语义映射不变，
门禁和自检通过。没有将此错误当成产品回归或省略原件。

本次仅接受组件。Legacy/Apple/Google其余格式、嵌入文档、全部API/
历史/后台/计数、转换来源、PDF视觉和真实表格摘要、业务与可回退发行
继续实施；未合main、未宣布A09.5或总目标完成。
