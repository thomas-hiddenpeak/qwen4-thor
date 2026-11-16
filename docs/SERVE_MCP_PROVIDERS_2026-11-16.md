# 原生连接器提供方适配

本项延续A07/A11/A12、R01/R05/R06及Q01—Q03。PR28的MCP与原生隧道
已经实际合main；将connector_id映射到外部MCP服务只是一种可选接入。
本项让q4t原生执行提供方API，不能把必须另外部署适配器当作完成。
默认rev134保持；以下新实现仍在开发和针对性验证，尚未发行或完成
真实提供方账户验收。全部33项Serve总目标保持active。

## 固定范围与来源

2026-11-16读取[OpenAI连接器协议](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)。
最新页面注明新模型的connector_id迁移到server_url/tunnel_id，但原模型
继续保留连接器。我们的既定兼容目标仍包含八个提供方，不因上游弃用
说明而自行删除。公开页面提供工具名与权限，除示例外未公开全部工具
参数细节；本地目录必须公开实际schema，不冒称验证了未公开内部合同。

| 提供方 | 本计划必须实现的工具 |
|---|---|
| Dropbox | search、fetch、search_files、fetch_file、list_recent_files、get_profile |
| Gmail | get_profile、search_emails、search_email_ids、get_recent_emails、read_email、batch_read_email |
| Google Calendar | get_profile、search、fetch、search_events、read_event |
| Google Drive | get_profile、list_drives、search、recent_documents、fetch |
| Microsoft Teams | search、fetch、get_chat_members、get_profile |
| Outlook Calendar | search_events、fetch_event、fetch_events_batch、list_events、get_profile |
| Outlook Email | get_profile、list_messages、search_messages、get_recent_emails、fetch_message、fetch_messages_batch |
| SharePoint | get_site、search、list_recent_documents、fetch、get_profile |

这些42个工具是实施清单，不是当前支持声明。先完成Google Calendar与
共用传输，随后Google Drive/Gmail、Microsoft Graph三组和Dropbox；
阶段是开发/验证/提交边界，不是全部提供方或总任务终点。

## 原生执行与失效范围

管理员在既有私有MCP策略中选择transport=provider，并精确配置
connector_id、调用方scope和generation；不接受任意目标URL。客户端
仍使用标准MCP声明与OAuth authorization，由应用负责取得授权令牌。
原server_url、HTTP映射和私网隧道继续可选，提供方适配不依赖外部MCP
程序、SDK或Web服务。不会读取本机浏览器凭据或替用户创建云授权。

复用隔离MCP worker、固定HTTPS主机与现有DNS/网络/系统TLS校验，
复用原目录/schema、审批、加密权限、持久续接、程序调用和Batch。
私有会话摘要增加provider_id，目标摘要区分transport及适配合同，
不能用旧外部MCP审批迁移到原生提供方。重启不重放外部调用。

请求共享绝对期限、32次HTTP操作和4MiB响应预算。禁止重定向、自动
HTTP重试、代理环境变量及模型参数选择域名。完整提供方HTTP错误
转换为已知工具错误，只给出状态码，不把可能含凭据的诊断正文回送。
连接中断保持不确定结果。401/403/429、DNS/TLS、时区库或提供方失能
只影响该工具请求，不改变核心readiness、不重启模型/服务树。

新增运行使用的只有可选提供方网络和系统C++时区数据；HTTPS与隔离
进程均复用已有实现。未配置/未调用时没有网络依赖。时区数据仅在
独立worker解析日历本地时间时加载；带显式UTC offset的时间不依赖
时区数据库。无需新增进程常驻或系统包作为核心LLM依赖。

## Google Calendar第一组

原生目录通过Google token-info返回的实际授权scope筛选工具；凭据
仅进入私有worker及Google官方端点。token-info要求的query令牌不
进入模型、普通输出或错误正文。事件接口同时接受只读scope，实际
访问仍由提供方校验权限。OAuth续期由调用应用处理，不隐式重放旧任务。

依据[事件列表](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)、
[单事件读取](https://developers.google.com/workspace/calendar/api/v3/reference/events/get)和
[用户信息](https://developers.google.com/identity/openid-connect/openid-connect)。
query、calendar_id、time_min/max、timezone_str、max_results、
next_page_token在实际目录中声明；search与search_events同义，fetch
与read_event使用event_id。空值和默认primary、50条明确处理；单页
上限2500，保持原始游标，不自动取遍所有页。原事件字段、ID、全天
日期、显示链接和时间细节保持，不伪造业务数据。

显式offset转UTC并保持小数；无offset时使用显式时区或同一calendar
事件列表返回的时区。元数据查询仅取timeZone，沿用事件读取权限，
不要求额外Calendars.get权限。DST重复/不存在的本地时间拒绝并要求
明确offset，不能悄悄采用服务器本地时区。日期范围不受纳秒时间点
约2262年的上限截断。系统时区数据不可用单列请求错误。

## 针对性验证与后续交付

### Google Drive与Gmail候选

依据[Drive文件查询](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)、
[下载](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/get)、
[导出格式](https://developers.google.com/workspace/drive/api/guides/ref-export-formats)及
[Gmail列表](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)、
[全文读取](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get)。
2026-11-16新增11工具候选，固定API主机、实际scope筛选沿用共享传输。
元数据权限只暴露对应目录，不借目录扩充OAuth实际权限。

Drive保留单页游标、incompleteSearch、原ID/名称/权限与共享盘选择。
关键词按字面量转义，资源key仅组成固定提供方头。fetch读取原字节，
Google文档/表格/幻灯片导出为docx/xlsx/pptx，不用仅首表CSV冒充完整
表格。快捷方式最多四跳且检测循环；不跟随提供方返回的任意下载URL。
目录返回明确folder元数据，子项由带folder_id的search单页读取。

Gmail使用当前授权用户，search_email_ids单页最多500，展开元数据
或batch最多20；列表顺序和游标保持。read_email处理完整MIME树，
外置正文自动读取，附件默认只保留元数据，include_attachments明确
请求字节。base64url严格解码，带charset正文经系统iconv转UTF-8；
原邮件ID/头/线程/日期保留，二进制进入原生资源。未知字符集失败
只影响该工具，不能自动把非法字节替换后宣称成功。batch保持输入
顺序（含重复ID），任一读取失败则整次报错，不伪造部分成功。

共用32次HTTP、4MiB原始响应及4MiB原生结果限制，绝对期限不变。
iconv复用系统libc且只在隔离worker调用，不引入常驻外部程序或
新链接密码库。真实提供方账户验收尚待测试资产；
[36项CPU与实际私有配置封包首过](evidence/serve-mcp-google-cpu-20261116/README.md)，
最终构建零警告、来源检查通过。Google Vids异步路径见下节；特殊格式
继续实现与验证，不能将一般文件下载证明成所有云端格式全覆盖。

### Google Vids长耗时下载候选

按Google官方[长耗时操作](https://developers.google.com/workspace/drive/api/guides/long-running-operations)、
[发起下载](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/download)、
[读取操作](https://developers.google.com/workspace/drive/api/reference/rest/v3/operations/get)与
[MIME类型](https://developers.google.com/workspace/drive/api/guides/mime-types)，
Google Vids走files.download导出MP4，不能复用files.export。
一次fetch仅发起一次；done为空/false时返回pending_download、
resume_arguments和建议等待秒数，不谎称已取得内容。后续fetch携带
原operation_name只查询一次原任务；初次已完成则直接取内容，不多查。

原操作名按不透明路径段编码，快捷方式续接绑定解析后的目标ID/key。
续接不查询另一份当前文件元数据来冒充原操作身份，结果明确标记
operation_name。服务方仍校验当前Google用户/权限；过期或撤权错误
不会重新发起下载。完整任务错误转成已知工具失败，诊断正文不外泄；
传输中断保持未知，无内部轮询、睡眠或自动重试。

仅认证操作返回的Google所属HTTPS下载URI可读取。部署域名/CA/公网
限制保留，私网放行不继承；不跟随重定向。OAuth只发固定Google API
主机，内容主机不接收OAuth或部署私有头；资源key只发受控Google主机。
下载URI/原始任务元数据不进入模型结果，HTML登录页不冒充视频。
复用32次HTTP、4MiB与绝对期限，无新依赖，也不影响核心健康状态。

30项针对性CPU首过，[来源与构建记录](evidence/serve-mcp-drive-lro-cpu-20261116/README.md)
保留，主机/完整引擎零警告；本机字节夹具仅证明传输、
错误和资源投影，不能证明真实Vids文件解码或云账户业务。实际资产、
下载主机组合、标准客户端及发行仍保留为待验，全部master继续。

### Microsoft Graph四提供方候选

依据Microsoft官方[邮件读取](https://learn.microsoft.com/en-us/graph/api/message-get?view=graph-rest-1.0)、
[日历视图](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0)、
[Teams消息](https://learn.microsoft.com/en-us/graph/api/chatmessage-get?view=graph-rest-1.0)、
[搜索](https://learn.microsoft.com/en-us/graph/api/resources/searchrequest?view=graph-rest-1.0)和
[文件下载](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0)，
Outlook Calendar五项、Email六项、Teams四项、SharePoint五项工具已接线。
Graph访问令牌按不透明凭据处理；目录只声明本机实际工具schema，不猜测
JWT内的权限。每个调用由Graph授权，get_profile单独需要User.Read；
没有该权限不会在目录阶段阻止已经获准的邮件/日历读取。

Graph API固定为graph.microsoft.com的v1.0。除固定只读search/query的
JSON POST外只发GET，不能通过工具参数写入邮件/消息/文件。保留opaque
ID、原始字段和分页URL；游标约束到原资源路径/域名，页大小、搜索词和
日历时间窗口不能悄悄变化。邮箱列表固定元数据投影，游标不能扩展正文
或附件；全文fetch需真实body，缺失则明确报错。邮件/事件批量最多20，
保持输入顺序和重复ID，中途失败不返回伪造的部分成功。

Outlook有范围查询用calendarView展开重复日程，日期偏移转UTC，返回
时区与重复实例关系保留。Teams读取聊天、频道消息/回复及成员，保留
租户和membership ID；删除消息的墓碑与无正文权限分别处理。搜索保留
Graph原始hit、summary、resource、offset和total含义，不把事件页计数
宣称成全局匹配数。

SharePoint搜索/最近文档使用Graph Search，最近明确为索引中的修改
时间降序；不依赖[已弃用的drive/recent](https://learn.microsoft.com/en-us/graph/api/drive-recent?view=graph-rest-1.0)
或把它冒充个人访问记录。fetch按item/drive/site读取真实文件，支持
SharePoint唯一ID和driveItem ID别名；remoteItem最多四跳并检测循环，
文件夹返回明确folder类型。

下载地址只能来自刚取得的、认证Graph响应的保留属性
@microsoft.graph.downloadUrl；不能由模型传入。下载使用HTTPS:443，
不转发OAuth、Cookie或任何部署私有头；沿用部署host/CA/公网限制，
清除私网放行且不跟随后续重定向。该地址是短期凭据，结果和搜索结果
递归清除它，原文件字节进入MCP resource。长度不符、超预算或下载错误
不产生部分成功。全部复用已有RemoteHttp/worker，无新增外部程序/库。

[40项不同C++最终通过](evidence/serve-mcp-graph-cpu-20261116/README.md)，
累计68次执行（Outlook首轮26、扩展组40、失败项补验2）；1项实际候选
CLI/私有封包检查及7项来源负控通过，最终主机/完整引擎零警告。两次
夹具编译错误及两项HTTP/HTTPS夹具失败原样保留，针对性修复，不扩大
GPU或全模型测试。七个提供方候选不等于真实账户/全部实时分页形式已验；
真实租户、权限、canonical分页别名与内容资产仍需受控账户验收。

### Dropbox六工具候选

依据官方[请求/响应格式](https://docs.dropboxapi.com/dropbox-api/docs/technical-reference/request-response-formats)、
[下载](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/download)、
[导出](https://docs.dropboxapi.com/dropbox-api/api-reference/user-endpoints/files/export)及
[API规范](https://github.com/dropbox/dropbox-api-spec/blob/master/files.stone)，
六项工具原生执行。RPC仅允许固定的profile/metadata/search/continue
四个POST端点；原文件/导出只从content.dropboxapi.com读取，写入和其他
主机拒绝。工具目录不发profile探测，文件读取不额外要求account权限；
服务方每次执行实际OAuth授权，API名称对应的工具和参数均明确公开。

搜索保留原始matches/metadata、has_more和cursor；续页只接受原游标，
不能同时传新查询/路径/页大小后静默忽略。最近文件明确是索引中的修改
时间顺序，排除文件夹，不冒充个人访问历史。单页最多1000，不自动扫完
账户或宣称索引结果绝对完整。

fetch/fetch_file接受Dropbox路径、id、namespace或revision。中文、
补充平面字符、合法换行路径经ASCII JSON编码进入Dropbox-API-Arg头；
不进入URL或shell。响应头Dropbox-API-Result按严格JSON解析并计入共享
预算；核对ID/revision和实际长度，字节进入MCP resource。文件类型标签
只是已有解码器的输入提示，不能替代解码验收。

直接下载先行，只有明确409 export-only才执行一次导出，明确not_file
才读取folder元数据。权限/限流/不确定传输不重试；409错误正文只提取
两种受控原因，不泄露原诊断；正文损坏或超时仍返回已经确定的409。
普通文件不因预先查询元数据而额外要求files.metadata.read。所有请求
共享原绝对期限、32次HTTP和4MiB预算，无新增外部程序/SDK/库。

[25项C++与1项实际CLI八提供方封包首过](evidence/serve-mcp-dropbox-cpu-20261116/README.md)，
来源7项负控及704锚点通过，主机/完整引擎零警告，0GPU。八提供方42工具
已有候选实现；真实账户、特殊格式、原生/标准客户端业务、恢复资格与
可回退发行/main仍继续，不能用数量齐全替代完整业务验收。

### 已验Calendar及后续

初始冻结27项CPU，另补具体能力声明后，28项不同C++最终通过；
14项不同Python/实际候选CLI与封包检查最终通过。覆盖原生部件/
真实本机HTTP、原权限/现代/旧SSE/隧道会话、scope目录、URI/游标、
时区、原事件字段、错误/无重试、共享预算、取消与配置权限。
首轮失败、针对性补验、精确候选身份见
[Calendar CPU证据](evidence/serve-mcp-provider-calendar-cpu-20261116/README.md)。
主机/完整引擎零警告，7项来源负控通过；没有启动模型或真实账户请求。

本机HTTP夹具只能证明实现与故障合同，不能证明Google账户/权限或
全部提供方的实时兼容。先完成可运行适配和私有配置入口，再使用
受控测试账户/资料做标准SDK业务、恢复资格与可回退交付；真实凭据
仅通过本机私有文件传入，不要求粘贴到聊天或提交到仓库。

尚未完成的提供方、实际账户业务、客户端/程序/Batch组合、来源
资格、发行/main与其余master均保留。非性能任务不扩展完整性能/
全模型质量矩阵，已验且未受影响证据直接复用，长稳实跑由用户负责。
