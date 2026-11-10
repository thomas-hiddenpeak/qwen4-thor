# A09.5 原生文件输入：全部实施项

起点为已交付Uploads引擎cdb5ced9/v11包96b75252，revision36、STOPPED。
本项完成后继续Batch/A10/A11及全部master任务；任何一个组件接受
不作为阶段停止点，也不能把解析器缺失改成最终接口限制。

## 合同与源码依据

固定官方Node SDK7.47.0的Responses/Chat content声明，并核对
[官方文件输入说明](https://developers.openai.com/api/docs/guides/file-inputs)。
源码身份保存在工作目录`.q4t-work/serve-file-inputs-20261110/source-review.json`。
Responses支持file_id/file_data/file_url；Chat标准file part支持PDF的
file_id/file_data，文本内容仍可由标准text part发送。Responses非PDF
提取文字；PDF同时交付提取文字与页面图像，detail影响页面处理。
表格每sheet处理前1000行，并提供真实生成的摘要/表头信息；这需要
明确计入模型步骤、用量和失败处理，不能把截取CSV冒充全部实现。

## 必须完成的子项

| 子项 | 实现及检查出口 |
|---|---|
| FI01 来源与合同 | 验证互斥来源、nullable/默认/detail/缓存标记；标准base64、真实File分块读取及SHA、客户端隔离、取消/期限、总字节预算；原始输入未验证前不做外部IO |
| FI02 原生获取与解析 | 可配置HTTP(S)获取、重定向/DNS/目标策略和上限；文本/代码、PDF文本与页面、Office/ODF/演示文稿及表格的实际提取；每类有真实产物与损坏/资源边界 |
| FI03 执行与持久性 | Chat/Responses、prompt变量、previous/conversation、background恢复、compact、input_tokens/preflight均使用同一来源/展开合同；保留原始输入与可核验来源，不把URL变化悄悄当原历史 |
| FI04 媒体与用量 | File image IDs交给实际视觉路径；PDF detail、页面/解码/内存预算、表格真实摘要步骤和用量；文件与工具/schema/缓存组合；配额超限和未配置能力不静默截取 |
| FI05 发行与真实闭环 | SDK标准输入、实际文档答案/引用、PDF页面视觉、后台/历史/计数一致及辅助故障同PID继续；针对性证据、依赖/许可、可回退发行、清理 |

FI03还须保留实际转换来源/系统解码器身份：Legacy表格中的公式结果
来自隔离转换后的文档，不能宣称是原二进制缓存；易变公式及格式转换
的来源须明确传给上下文和持久快照。此项尚未接API，保持实施义务。

## 依赖与资源设计

文件归属/读取复用已验ObjectStore和不可变分块，不从客户端filename
打开主机路径。预算按实际解码字节累计，元数据不作为内容完整性证据。
单请求文件原字节的本地明确上限为50000000字节，输出文字、页面、
解码内存和HTTP body部署容量另外受限，发现/config必须区分这些层。

新获取/解析执行域与模型、grammar、对象IO和缓存IO分开；截止、取消、
清理及未确认退出占槽遵守现有进程所有权合同，不按超时增殖worker。
文本/标准容器优先自有实现；复杂PDF/Office重用已有成熟编解码组件，
以自有有界子进程或库适配，不启动外部文档服务。具体依赖、许可证、
身份和打包检查登记；不能因缺库让整个引擎无法启动或readiness下降。
外部组件失能只影响依赖它的请求，普通文本继续；缺失格式仍保留实现
任务，不把unsupported或可选开关当最终完成。解析器不得运行宏、脚本、
外链或任意命令，网络获取单独授权且每次跳转验证目标。

## 实施与验证顺序

先成组实现FI01来源合同/受控读取及FI02解析底座，直接CPU检查真实
字节、损坏/隔离/期限/引用与资源边界。随后接FI03/FI04全部执行入口，
冻结针对性CPU及必要模型短验后成组验收。具体用例/启动上限在执行前
记录；已过且身份未变的证据复用。非性能工作，不跑完整性能或整模型
质量矩阵。长稳仍由用户执行，工具与交接属于本任务。

## FI02 原生获取组件（2026-11-10）

实现使用本项目HTTP字段/分块合同及独立exec工作进程，不调用下载
程序、代理环境或外部文件服务。TLS复用已有系统OpenSSL 3开发头及
libssl；仅HTTPS工作进程动态加载，缺库/证书/CA/DNS只失败该文件请求。
依据[OpenSSL主机名校验](https://docs.openssl.org/3.0/man3/SSL_set1_host/)和
[信任根配置](https://docs.openssl.org/3.0/man3/SSL_CTX_load_verify_locations/)，
启用peer验证、DNS或IP校验、TLS1.2下限、HTTP/1.1 ALPN，禁止HTTPS
跳转降为HTTP。没有复制第三方服务器代码或新增服务进程依赖。

部署可设置精确主机、CIDR、端口、CA和总超时；请求不能覆盖它们。
默认仅公网及80/443，按[IANA IPv4](https://www.iana.org/assignments/iana-ipv4-special-registry/)
和[IPv6特殊地址登记](https://www.iana.org/assignments/iana-ipv6-special-registry/)
保守拒绝特殊/私网/转换地址；显式CIDR可授权内网，映射IPv4按IPv4
规则核对。每跳最多16个解析结果，全部经验证后直接连接其数值地址，
没有二次主机名解析；每跳重新授权，不转发cookie/API Key/其他凭据。

完整200内容按[HTTP/1.1分帧](https://www.rfc-editor.org/rfc/rfc9112.html)
读取CL、chunked或连接结束；拒绝歧义、截断、部分文件及保护字段的
迟到trailer，要求identity编码。控制头32KiB、分块控制2MiB、原字节
不超过请求剩余预算；最多10跳/30秒，默认5跳/15秒。失败或未知回执
保留本次最大尝试预算，成功按实际字节计费。独立固定槽位在未确认
退出时保持占用，native loop也回收已退出子进程，不等待下一次文件请求。

16项C++及4本机TLS实网首组通过；清单生成缩进导致执行前拒绝的
原件保留。增加loop回收绑定后仅补1直接用例，其他15项与TLS身份
依赖未变证据复用。完整文档解析、标准API执行、持久快照和真实模型
业务仍继续实施，不能据组件通过标记A09.5完成。

## FI02 文本表格及独立解析（2026-11-10）

[10项纯解析](evidence/serve-file-text-cpu-20261110/README.md)与
[10项独立进程](evidence/serve-file-parser-worker-cpu-20261110/README.md)
已经接受。原生UTF8/BOM UTF16/32、CSV/TSV/IIF保留真实内容，表格只
留前1000行并统计全部行；可配置文字/单元格/结构预算，默认4MiB文字、
1MiB单元格、1024列/131072单元格，上限16MiB/4MiB/16384列/1000000格。
表格中间UTF8不超过50000000字节。没有伪造摘要或表头推理。

独立kFileParse进程执行纯内存解析，最多10秒、2GiB地址空间；继承
既有fd3所有权与100ms清理合同。seccomp在已exec/验证后按syscall
白名单限制，禁文件/网络/进程创建/exec及执行内存；失败不降为无保护
解析，不改变核心readiness或启动模型。主循环回收延迟退出的worker。

PDF/Office/RTF继续实现专用解码路径与对应资源限制，不能把纯文本
沙箱证据扩成这些解码器接受。全部格式的API/历史/后台/计数、持久
来源、表格模型摘要/用量及真实业务和可回退发行仍在FI03—FI05。

## FI02 PDF解码执行设计（实施中）

本机只读环境核对：kernel6.8.12-1021-tegra、Landlock ABI4；系统
pdfinfo/pdftotext/pdftoppm24.02.0存在，原件保存在工作目录
pdf-environment.json与各help.txt。该查询不做模型加载/GPU，也不是PDF
解码验收。复杂PDF优先使用系统独立编解码程序的标准输入输出，不把
GPL解码库链接进MIT核心，不启动外部文档服务或复制解码器源码。
系统组件的依赖、版本、许可证及失效范围在实际接入时登记。

q4t自有worker接收已授权原字节到只读memfd，选择固定解码器/参数并
exec替换自身；无shell、无客户路径、无额外孙进程。文字提取和每页
图像都须真正完成，页数/像素/总输出/期限有界；不能只取PDF文字。
父进程要同时确认原始输出EOF和该owned子进程正常退出，不能把收到
部分stdout或SIGKILL清理当解码成功。因此先补现有process抽象的有界
读至EOF/正常退出确认，再接实际PDF文本、页面与损坏/隔离检查。

复杂解码需读取系统库/字体，采用已核对的Landlock文件读取许可及
seccomp限制，与纯内存文本沙箱分开验证。缺失解码器或隔离条件时
仅拒绝该依赖请求，不停止/重启健康模型；不能以这种降级作为PDF
实现或验收完成。后续Office/RTF与全部FI03—FI05继续。

PDF组件完成[18 C++与1真实像素检查](evidence/serve-file-pdf-cpu-20261110/README.md)，保留首次EACCES失败、诊断及受影响补验。固定工具inode只读+执行、库目录只读，EOF与正常退出共同确认成功；实际全部文字/页面、旋转、预算和取消已核对。默认32页/64M像素/64MiB图片、每页16MiB；总30秒可配置至120秒，2GiB单worker地址空间，model/context准入仍在FI03。未接API/真实模型或默认发行；下一步Office/RTF，之后全部消费与业务任务。

## FI02 Office容器与语义接入（继续实施）

容器复用系统[libarchive内存读取接口](https://github.com/libarchive/libarchive/blob/v3.7.2/libarchive/archive_read.3)
与[libxml2 XML reader](https://gnome.pages.gitlab.gnome.org/libxml2/html/xmlreader_8h.html)。
本机实际3.7.2/2.9.14；只采用本机头文件已有接口，不假设最新版API。
两个库只在独立解析worker内预加载，随后进入既有纯内存seccomp；
核心启动没有新的DT_NEEDED或文档服务进程。dev头只解压到工作目录，
系统安装未变；正常构建依赖及公共host检查所需包已登记README/CI。

seekable ZIP要求完整目录，所有成员按实际字节/CRC读取，包括被格式
忽略的数据；默认128MiB展开/32MiB成员/4096成员，可至512MiB/64MiB/
65536成员。重复/穿越/链接/加密拒绝，不写包内路径。XML按namespace
展开名称，保留Unicode/空白，拒绝DTD与外部/自定义实体；固定外部
loader拒绝读取。默认1000000节点/深度64，硬上限2000000/128，
节点/属性/字节均有界，取消和错误停止访问，最终文档仅成功后发布。

格式语义继续依据[WordprocessingML](https://learn.microsoft.com/en-us/office/open-xml/word/structure-of-a-wordprocessingml-document)、
[SpreadsheetML](https://learn.microsoft.com/en-us/office/open-xml/spreadsheet/structure-of-a-spreadsheetml-document)
及[ODF 1.2](https://docs.oasis-open.org/office/v1.2/os/OpenDocument-v1.2-os-part1.html)：
文档段落/表格、演示文稿实际顺序、workbook关联和共享字符串、真实行列/
1000行及缓存数值、ODF重复行列/文字都须实现。Legacy Office、RTF、
Apple及Google文档来源适配继续保留，容器接受不替代它们或全部FI03—FI05。

Office/RTF已完成[26项首组与3项依赖隔离补验](evidence/serve-file-office-cpu-20261110/README.md)。
Word/PPT/XLSX/ODF及RTF实际内容在生产独立worker内处理；兼容分支仅
选择一次，外部关系不读取，公式保留实际表达式/缓存/格式而不执行。
RTF通过系统libc iconv预加载代码页，无新增核心链接依赖；缺失未使用
代码页不阻断其他文档，实际转换缺失仅失败对应请求。文档时限默认
10秒可配至120秒，工作表1—256、嵌套0—4，包和输出预算同时复核。
本组仅组件接受，尚未发布默认；Legacy/Apple/Google和所有消费/历史/
模型/业务任务继续，完整文件输入尚未交付。

旧格式解码底座的[18项内核IO预算检查](evidence/serve-file-codec-budget-cpu-20261110/README.md)
已通过，首次夹具Werror与修复保留。独立worker将通过seccomp通知在
系统调用执行前限制累计写入/声明长度、文件和线程创建；父端按单一
owned进程处理通知、截止/取消及EOF后清理，不执行解码器的文件操作。
完整转换/沙箱及私有目录清理仍在实施，不能据预算组件接受结束FI02。

## FI02旧格式转换组件（2026-11-10）

[14项CPU组件接受](evidence/serve-file-legacy-cpu-20261110/README.md)，真实DOC/XLS/PPT、原文件SHA、资源/取消/目录/沙箱及ODF稀疏行与绘图页通过。保留首失败、两次定向诊断和分依赖复用记录；候选3b67937c、0GPU，默认Uploads包不变。系统LibreOfficeKit仅在owned子进程动态加载，无新常驻服务或核心DT_NEEDED。旧格式转换结果完整且正常退出后清理、再解析；宏/链接关闭，网络/GPU/外部exec拒绝，私有目录有界写入。Apple/Google及嵌入格式、FI03—FI05和全部总任务继续。

统一来源/分派已完成[24项CPU及Google公开样表真实导出](evidence/serve-file-adapters-cpu-20261110/README.md)。Apple/XLSB实际内容、Google快捷方式与累计字节/私有来源、系统转换版本/BuildId和双SHA均已进入组件；API快照仍须保留这些信息。嵌入文档、全部FI03—FI05和总任务继续实施，不据组件接受结案。

嵌入文档组件[30项针对性CPU接受](evidence/serve-file-nested-cpu-20261110/README.md)：实际DOC/PDF多层内容、完整私有回执、整树预算/期限及取消/原子失败通过；并补签名优先和压缩iWork。候选136ecbaa未发行，0GPU，继续FI03—FI05所有消费/持久/模型/业务与全部总任务。

## FI03 私有快照组件与发行义务

[21项不同CPU接受](evidence/serve-input-snapshot-cpu-20261110/README.md)。
私有input_snapshot对象保存真实元数据/字节/摘要，复用1MiB有配额
分块，准备租约与发布原子提交；应用私有引用接管生命周期，失去
全部引用后可回收。丢回执保留ID和不确定状态，不重放。默认总量
128MiB、元数据16MiB/1024段，部署可至512MiB/32MiB，不能替代
请求全局RAM/原字节准入。具体文档/全部消费入口接线仍继续实施。

FI05必须提供能识别input_snapshot存储类型的配套回退程序；旧
Uploads二进制不能直接用于已有新类型的目录。不得删除新业务
历史来伪造回退成功。当前默认与其状态目录未改，组件候选未发行。

具体文档快照[6项CPU接受](evidence/serve-file-snapshot-cpu-20261110/README.md)：真实嵌入DOC/PDF、XLSX/XLS/Numbers、原File删除及解码不可用/重启后仍保留原内容；严格类型和现行预算复核。官方表格模型生成合同再次核对，摘要/表头、用量及计数一致性仍须实际接入，不能用纯CSV截取代替。所有API/业务/发行及总任务继续。
