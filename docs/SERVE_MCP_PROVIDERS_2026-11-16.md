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
