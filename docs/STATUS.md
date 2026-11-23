# 当前状态（2026-11-24）

Serve总目标仍为 **active**。按[完整计划](SERVE_MASTER_PLAN_2026-11-08.md)
和[逐项任务表](serve-master-goals-v2.json)完成全部33项任务；阶段验证、
发行和PR合入都不是总任务终点。非性能改动只做受影响行为的针对性
验证与必要业务短验，长稳实跑与已延期听感由用户负责。

## 正在推进

Agent 会话候选已通过官方 SDK 的延迟函数发现与回填。模型实际搜索并调用 read_release_record；客户端在回填前撤销函数，服务仍接受原调用的结果，模型回答真实发行记录中的 452。跨客户回填为 404，独立 Chat 答 42。完成 49 项针对性 CPU 测试和一次 41.543 秒的原生 TLS 短验。函数撤销和同名参数替换在下一次模型调用生效；会话 API 只展示标准可见项目。当前默认仍为 rev454。继续核对多步工具回合的流式输出下标及执行能力。[候选验收](evidence/serve-agent-workspaces-20261124/tool-search.json)

默认发行已切换至 **rev454**，版本登记为 156，服务意图为 STOPPED。模型退出及网关故障接管分别用时 123.2 和 158.3 秒。两次均重新下载到原 384 KiB 产物，摘要一致，原会话工具回合也完成回填，随后独立 Chat 回答 42。发布启动模型 0 次，13 份受保护文件一致。24 小时计划已预检，留给用户实跑。[发行与恢复回执](evidence/serve-agent-workspaces-20261124/artifact-delivery.json)

逐方法核对修正了一处范围计数。157 条保留路径共有 232 个 HTTP 方法；其中 `DELETE /models/{model}` 仅供组织所有者删除微调模型，与推理所需的 GET 共用路径。按用户已确定的平台管理范围，推理与应用必做 231 个，默认包已声明 126 个，成功状态集合均与冻结规范一致。此次新增会话产物的列表、元数据、内容下载和删除。105 个方法仍缺声明，托管执行器生成文件与 Agent 子会话等业务仍待实现。接下来先验证会话延迟加载函数和更新工具后的调用范围。[逐方法对照](evidence/serve-agent-workspaces-20261124/artifact-methods.json)

主、轻配置已接续同一工具回合和 384 KiB 工作区文件。主配置在删除 Files 源对象后进入工具等待，轻配置完成回填和后续回合。两个独立的产物内容都与源文件一致，覆盖工作区文件或删除一个产物后，另一个仍可读取。两配置各启动一次，分别耗时 47.1 秒和 126.2 秒。28 项迁移测试通过，两份新资格均可逐字节还原 rev452 的前驱记录。继续封包并检验模型退出和网关接管后的产物下载。[配置接续回执](evidence/serve-agent-workspaces-20261124/artifact-profiles.json)

PR #73 两项远端 host-contracts 检查均通过，已合入 main（9d5a63543），合并树与受检提交一致。主线包含 rev452 的交付记录和按解码字节检查内联文件上限的修复。会话产物及 50 MiB Files 源文件另见 [PR #74](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/74)。[主线回执](evidence/serve-agent-workspaces-20261124/workspace-main.json)

托管工作区已按每个 Files 源文件独立执行 50 MiB 上限。官方 SDK 在原生 TLS 下复制恰好 50 MiB，再放入 1 字节文件；删除 Files 源对象后，回合产物仍下载到 52,428,800 字节，摘要一致。超过 1 字节在预热及已连接工作区均返回 400。首次短验在会话删除后得到 504，现场日志记录了一笔回收 128 项的事务。修复保留现有 2 秒对象期限，直接校验已登记记录的序列化字节；同一类 128 分块 CPU 查询从 2942.205 毫秒降到 397.659 毫秒。复验用时 71.364 秒，删除后立即查询为 404，后续 Chat 答 42，112 项 CPU 检查通过。该短验使用 512 MiB 客户存储配额及 64 MiB 入口，默认客户存储配额为 64 MiB。继续主轻配置接续与可回退交付。[大文件及回收回执](evidence/serve-agent-workspaces-20261124/source-files.json)

会话产物的列表、元数据、二进制下载和删除四项操作已通过官方 SDK 经原生 TLS 的真实业务短验（38.096 秒）。模型等待工具时产物为 0，回填后回答 rev452，并发布客户端放入工作区的 19,120 字节发行回执和空文件。第二回合同名文件含 NUL 字节，旧副本仍逐字节相同。删除新副本后工作区源文件仍在，删除会话后旧副本返回 404。62 项相关 CPU 用例通过，覆盖原子写冲突、跨客户及回收依赖。候选声明 127/232 项，默认 rev452 仍为 123 项。继续处理大文件和执行器写入；两次验收脚本失败另存。[产物回执](evidence/serve-agent-workspaces-20261124/artifacts.json)

PR #72 两项远端检查均通过，已合入 main（433fa211f），合并树与受检提交相同。面向托管工作区的下一次提交还补齐了网关通用路由测试里的环境 ID。真实 nginx 将所有登记路由通过正确端口转发，逐条保留方法、查询及客户身份。[主线回执](evidence/serve-agent-environments-20261124/contract-main.json)

托管环境上传已按解码字节检查每个内联文件 5 MiB、每个创建或更新请求 10 MiB。5 MiB 加 1 字节与恰好 5 MiB 的 base64 长度相同，旧的字符上限无法区分。10 项 CPU 检查及官方 SDK 经 TLS 的单次模型短验通过（41.298 秒）。恰好 10 MiB 可创建；模板创建、更新、预热、会话初始化和连接后的文件写入五种超限请求均返回 400 及对应字段，原文件保留，Chat 回答 42。此修复仍在工作分支，默认 rev452。继续补齐会话产物。[验收数据](evidence/serve-agent-workspaces-20261124/inline-limits.json)

默认发行已从 rev450 切换至 **rev452**，版本登记为 155。独立回退包的模型退出与主入口故障接管分别用时 120.871 和 154.303 秒。两次均接续原工具回合、恢复并替换 384 KiB 文件，后续 Chat 回答 42。发布过程启动模型 0 次，服务意图为 STOPPED，13 份受保护文件一致。24 小时计划已预检，实跑由用户安排。[发行与恢复回执](evidence/serve-agent-workspaces-20261124/delivery.json)

对实际发行包复核，推理与应用服务的 232 个 HTTP 方法已声明 123 个，其中 122 个与官方成功状态码一致。新增环境创建、查询、环境列表及文件创建、列表 5 项；尚缺声明 109 项，已有声明的操作也需按业务语义验收。下一组覆盖工作区文件输入上限与会话产物。[逐方法对照](evidence/serve-agent-workspaces-20261124/methods.json)

分块快照修复版已取得主、轻两份独立资格。主配置用时 46.1 秒，轻配置用时 118.0 秒，各启动一次；同一工具回合及工作区在轻配置完成回填。保留已验证的前驱和全部原资格，继续大文件跨故障恢复与封包。[资格回执](evidence/serve-agent-workspaces-20261124/payload-profiles.json)

存储写入、提交校验和回收器现在共用可分块对象类型表。回收器按已登记的位置识别长字符串的快照引用，涵盖 Agent 指令和大模板密文。46 项 CPU 用例通过，含重开后恢复 384 KiB 指令及逐字节校验 5 MiB 文件。官方 SDK 与原生在线回收命令在同一已启动服务中验收，恰好删除 1 个孤立项，5 MiB 模板和已挂接工作区都能重新打开，后续推理回答 42。单次启动 45.147 秒。带此修复的包继续托管资格验证。[在线回收与 CPU 原件](evidence/serve-agent-workspaces-20261124/payload-retention.json)

回收修复版的主、轻配置接续了同一会话、文件和待回填工具调用。主配置用时 47.5 秒，轻配置用时 119.0 秒，各启动一次。两份资格均还原了 rev450 的完整前驱记录，27 项迁移测试通过。继续独立回退包和主入口故障接管。[资格回执](evidence/serve-agent-workspaces-20261124/retention-profiles.json)

托管环境回收修复已通过 33 项针对性 CPU 用例。真实对象库重开后，回收器准确选中并删除一个同租户孤立项，空闲会话仍能读到原文件，模板仍可读取。官方 SDK 经原生 TLS 完成一次 35.191 秒的模型短验，删除源文件后工作区可用，最终回答 42。此前候选已通过模型退出和网关接管；本次重新登记修复版的主、轻配置及回退包，默认仍为 rev450。[回收和真实业务回执](evidence/serve-agent-workspaces-20261124/retention.json)

主、轻配置已接续同一托管工作区和待回填的工具调用。两次启动分别耗时 46.4 秒和 118.7 秒；工具等待期间文件写入返回 409。轻配置完成回填后替换了原文件，删除会话后读到 disconnected。两份新资格均可逐字节还原 rev450 的记录，登记和复核未再启动模型。下一步是封包并验证模型退出和入口接管。[主轻资格](evidence/serve-agent-workspaces-20261124/profiles.json)

托管工作区候选已通过官方 SDK 的原生 TLS 短验。预热时复制 18,135 字节的真实发布记录，删除源 Files 对象和模板后仍可挂接会话。SDK 验证了文件替换、两页目录读取、跨租户 404、重复认领 409；会话首次推理回答 42，删除会话后环境进入 disconnected。1 次模型启动，34.454 秒。18 项相关 CPU 用例通过，覆盖存储重开后的字节内容及写入与首回合竞争。候选现声明 123/232 个方法，默认仍为 rev450。下一步验证主、轻配置接续同一工作区，并补齐托管执行和产物读回。[验收回执](evidence/serve-agent-workspaces-20261124/workspace.json)

PR #72 第二轮 CI 在跟踪存储恢复用例失败。生产会话会在空闲循环调用 Tick，测试在首次投递遇到争用后只读取健康状态，不能驱动尚未提交的快照。夹具现明确让第一次取不到 exporter，再用真实 Tick 提交，观察写入失败后修复目录，校验同一快照完成持久化。连续 3 次通过，保留远端失败记录。[远端检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/45244553659/job/137937768606)

PR #72 的两条 CI 均在同一条网关测试失败。该测试读取会话创建的 HTTP 200 项；原生 JSON 和 SSE 短验以及冻结规范均为 201。已将断言改为成功状态集合恰为 201，并继续检查两种媒体类型。网关 19 项测试通过，来源审计 38 项反例通过。等待远端对修复提交的两条检查。

PR #71 已通过两项 host-contracts 检查并合入 main（eb7f5064d）。合并树与受检提交 0a0a54b19 相同。主线已包含环境模板和主、轻配置资格；rev450 发布回执及后续 HTTP 合同修复另行提交。[合并回执](evidence/serve-agent-environments-20261124/main.json)

候选已通过 5 MiB 内联文件的真实模板验收。SDK 提交 6,990,508 个 base64 字符，创建、读取、修改名称、清空和删除成功；超上限返回 400，原数据仍在。重新打开存储后逐字节还原文件，对调密文分段、截断或换模板均被拒绝。单次模型启动 41.320 秒，随后 Chat 答 42。首次租约因我方 CPU 测试并行被保护机制中止，保留原记录，CPU 完成后在同一规则下串行重验。下一步是让环境实例使用模板和项目工作区文件。[短验与首次中止](evidence/serve-agent-environments-20261124/large-inline.json)

冻结的 157 条推理与应用路径包含 **232 个 HTTP 方法**。rev450 声明 118 项，114 项缺少声明。候选已将五类创建操作的实际响应修正为 201；同一 API 的流式创建也返回 201，模板更新返回 200。官方与本地模板输入边界 32 例吻合，真实 TLS 业务用时 38.547 秒。其余一个成功码差异涉及微调模型删除与本机的只读模型边界。继续执行环境与其他必做接口。[方法矩阵与真实回执](evidence/serve-method-contracts-20261124/README.md)

默认已交付 **rev450**，生产服务保持 **STOPPED**。回退包模型退出与主网关故障接管分别用时 120.425 和 145.957 秒，各启动两代模型。两次恢复后均可读取原模板、接续同一工具回合，跟踪含三条 span，跨客户操作返回 404，随后 Chat 答 42。发布过程启动模型 0 次，13 份受保护文件逐字节一致。24 小时计划已完成预检，实跑由用户安排。下一项是逐方法 HTTP 合同及环境实例。[发行与回退回执](evidence/serve-agent-environments-20261124/delivery.json)

PR #70 已合入 main（cbbe6fddc）。两个 host-contracts 检查通过，合并树与受检提交相同。主线已包含 rev448 交付记录和会话跟踪实现。[主线回执](evidence/serve-agent-vaults-20261123/oauth-main.json)

执行环境与 Vault 类型图已补入发行包的 SDK 来源说明。当前候选继续托管恢复和可回退封包。[SDK 来源](../third_party/licenses/openai-node/README.md)

主、轻配置已接续同一份保密环境模板和等待回填的工具回合。轻配置读到完全相同的模板和步骤，成功修改并删除模板；工具回填后生成 3 个 span，随后分页读到配额拒绝的错误 span。两配置各启动一次，耗时 46.610 和 119.106 秒。新资格可还原 rev448 的两份记录。继续验证模型退出与网关接管。[主轻资格](evidence/serve-agent-environments-20261124/profiles.json)

模板网络策略已补验。先前候选接受了同时非空的允许列表和阻止列表；修复后同一模板上的三笔非法更新均返回 400，原配置仍可读取。受限网络下的精确阻止域名可保存。创建、更新的 OpenAPI 合同共验证 12 个例子，真实 SDK 回归用时 34.925 秒，Chat 答 42。[网络合同与原失败](evidence/serve-agent-environments-20261124/template-network.json)

执行环境模板的五项操作已通过官方 SDK 及原生 TLS 短验。客户上传真实 Files 对象、引入 57 字节内联文件、更新模板并分页读取两条记录。其他客户引用该文件、读取或删除模板均为 404。公开响应隐藏环境值、设置命令和文件内容；存储重开后可更新，跨模板解密被拒绝。单次模型启动 33.952 秒，随后 Chat 回答 42。候选公开声明为 80/157 项，默认 rev448 为 77/157 项。继续实现环境实例和工作区文件。[模板验收](evidence/serve-agent-environments-20261124/templates.json)

PR #69 已合入 main（30c537220）。两个 host-contracts 均通过，合并树与受检提交 e8132e951 相同。主线包含 rev446 交付及 OAuth 签发端临时故障处理。[主线回执](evidence/serve-agent-vaults-20261123/recovery-main.json)

官方 SDK 经原生 TLS 读取真实工具回合的跟踪。等待回填时读到 1 个已完成步骤，工具返回后读到 2 个步骤和 1 个根 span。跨回合分页及跨客户 404 通过。模型启动一次，用时 39.223 秒，查询历史发行记录得到 442。存储重开后跟踪逐字节一致，原生与可选 nginx 入口均验证了分页参数及身份。候选公开声明覆盖 78/157 个范围条目，默认 rev448 为 77/157。继续主轻配置验收及执行环境接入。[会话跟踪验收](evidence/serve-agent-sessions-20261123/traces.json)

默认已交付 **rev448**，服务意图保持 **STOPPED**。两次独立故障短验分别终止模型和主网关，各启动两代模型。接管后凭据可轮换，跨客户读取返回 404，重复回填只保存一份结果，随后 Chat 均回答 42。默认策略在连接前拒绝未配置的令牌签发端。独立回退包已注册，发布过程启动模型 0 次，13 份受保护文件逐字节一致。24 小时计划已预检，实跑交给用户。[发行、故障和交接回执](evidence/serve-agent-vaults-20261123/oauth-delivery.json)

OAuth 刷新候选已取得主、轻配置资格。主配置保存过期的 OAuth 凭据，轻配置的 SDK 只接收原 Vault 和凭据 ID，在同一个 HTTPS MCP 会话内以三个访问令牌完成一次工具调用和一次资源读取。两套配置各启动一次，分别用时 46.420 和 119.064 秒。25 项迁移 CPU 用例通过，新资格可还原 rev446 的两份原记录。首次离线准备遗漏已安装 worker 的目录，补上原路径后完成短验。下一步验证候选服务包的故障接管。[资格回执](evidence/serve-agent-vaults-20261123/oauth-profiles.json)

同一 MCP 会话已跨过两次真实令牌到期。SDK 读项目文档时，工具列出、调用、关联资源读取分别使用了三个不同的访问令牌。对端只初始化一次，工具和资源各执行一次，最后的未知回执也未被重放。22 项针对性 CPU 检查和一次 48.923 秒的模型短验通过。用户主动更改凭据会产生新的授权；旧版会话可在原凭据有效期间接续。接着做主、轻配置验收，默认仍是 rev446。[逐次交换与业务回执](evidence/serve-agent-vaults-20261123/oauth-session.json)

OAuth 刷新候选已验证临时故障恢复。HTTPS 签发端要求等待 4 秒，两次 SDK 请求均收到 503，签发端只收到一次请求；等待期过后，原凭据成功续期，模型读回项目文档。随后丢失一次轮换回执，下一次请求得到 409，Chat 仍正常。16 项 CPU 检查通过，模型启动一次，短验用时 49.949 秒。继续验证执行中的续期和受管配置。[短验回执](evidence/serve-agent-vaults-20261123/oauth-temporary.json)

PR #68 已合入主线。两项远端 host-contracts 通过；合并提交 `004153a4d` 与受检提交文件树相同，包含 Vault 的创建、归档、凭据轮换和主、轻配置业务接续。[主线回执](evidence/serve-agent-vaults-20261123/main.json)

OAuth 自动刷新候选通过官方 SDK 业务短验。隔离的 HTTPS 令牌端点签发新访问令牌和轮换令牌，模型用新授权读取项目文档，准确引用第 1–3 行。第二次交换消耗了轮换令牌并丢失回执，重试收到 409，对端观察到总共两次交换，随后 Chat 正常回答 42。15 项 CPU 检查通过，单次模型启动 46.885 秒。继续验证临时错误重试和执行中令牌续期；默认 rev446。[OAuth 验收](evidence/serve-agent-vaults-20261123/oauth.json)

默认已交付 **rev446，STOPPED**。在隔离托管服务分别终止模型和主网关后，新进程均保留原客户的 Vault 凭据；轮换成功，其他客户删除为 404，归档后禁止新绑定。相同工具结果回填两次只保存一份，随后 Chat 回答 42。发布未启动生产模型，13 份受保护文件保持原字节，独立回退包已注册。24 小时计划已预检，留给用户执行。[发行与交接](evidence/serve-agent-vaults-20261123/delivery.json)

主配置保存的 Vault 凭据已由轻配置实际接续。轻配置只收到原凭据 ID 和服务地址，经 HTTPS 读取项目资料并准确引用第 1–3 行；归档后新绑定、跨租户读取均为 404。两套配置各启动一次，153 次 NVML 采样正常。独立 OpenAPI 已达 2,126,486 字节，封包读取边界已修复，复用了同一批业务原件。默认仍为 rev444、STOPPED；继续托管故障接管与可回退发行。[主轻资格](evidence/serve-agent-vaults-20261123/profiles.json)

PR #67 的两项远端 host-contracts 均已通过。rev444 的托管恢复、独立回退包和长稳交接已进入 main；合并提交 c3a9bd91d 与受检提交 8b3df89ea 的文件树一致。[主线回执](evidence/serve-agent-sessions-20261123/recovery-main.json)

官方 SDK 通过原生 TLS 和带凭据的 HTTPS MCP 读取了本项目文档第 1–3 行，回答准确引用 `[docs/MODEL.md:L1-L3]`。对端记录 1 次工具调用和 1 次资源读取；错租户、错目标及归档返回 404，自动选择遇到两份匹配凭据返回 409。后台目录误阻止推理和续答误用采样索引的两次原失败均已修复。29 项针对性 CPU 用例通过，最终单启动 46.750 秒，默认仍为 rev444。后续继续 OAuth 刷新、托管环境及主轻配置交付。[执行验收](evidence/serve-agent-vaults-20261123/mcp.json)

PR #67 首轮一项远端检查在快照清理丢回执的故障用例开始失败，随后多项测试无法取得对象控制器。夹具原先只等排队请求清空，且把健康替代进程的启动也限为 100ms。现等待实际回收计数与启动数一致，健康操作使用正常时限，真实丢回执仍受独立的 250ms 清理时限约束。39 项快照及上传回归均通过，保留远端原失败。[复验回执](evidence/serve-agent-sessions-20261123/snapshot-reap.json)

Vault 凭据的 5 项管理操作已通过官方 SDK 和原生 TLS。静态 Bearer、OAuth 与环境变量凭据可创建、轮换、按元数据分页并归档；公开响应隐藏令牌，父 Vault 归档时原子撤销三份凭据。换客户或 Vault 读取为 404，切换认证方式为 400。一次真实启动后 Chat 回答 42。候选公开声明覆盖 77/157 个范围条目，默认 rev444 保持 STOPPED。下一步验收实际 MCP 目的地上的授权使用。[凭据验收](evidence/serve-agent-vaults-20261123/credentials.json)

Vault 候选经官方 Node SDK 和原生 TLS 完成创建、筛选分页、更新和归档。真实模型在绑定 Vault 的会话回答 42；其他客户读取及绑定为 404，归档后新绑定为 404，旧会话仍可回读原引用。单启动 34.449 秒。候选 OpenAPI 新增 2 条路径、5 项操作，G00 的 157 项中已有 75 项具备公开操作声明。当前默认仍是 rev444；继续凭据子资源及与执行器的接入。[验收回执](evidence/serve-agent-vaults-20261123/acceptance.json)

默认已发布 **rev444**，服务意图为 **STOPPED**。模型进程退出、主网关致命退出两种实际托管故障各启动两代模型。新进程均接续原回合待回填的工具，同一结果提交两次只保存一份，其他客户读取为 404，随后 Chat 回答 42。回退包已注册，发布前后客户端与恢复状态的 13 份文件逐字节一致。24 小时计划已预检，由用户运行。[发行、故障与交接回执](evidence/serve-agent-sessions-20261123/delivery.json)

PR #66 的两项远端检查通过，合并提交 `5806ed388` 与受检提交文件树相同。G00 冻结的 157 个推理与应用路径或选择器中，发行 OpenAPI 已声明其中 73 个的至少一项操作，另 84 个尚无匹配声明。此计数不代表逐方法业务验收。88 个云平台管理条目继续按用户决定排除。[主线回执](evidence/serve-agent-sessions-20261123/main.json) · [逐条对照](evidence/serve-agent-sessions-20261123/scope-routes.json)

主配置生成的真实工具调用已由轻配置接续。官方 Node SDK 用同一结果回填两次，原回合只保存一份结果并回答 442。保留夹具的 3 RPM 上限后，第 4 笔推理准确返回 rate_limit_exceeded，原回合的失败可回读，会话仍可使用。窗口恢复后续问再答 442。主、轻各启动一次，146 次 NVML 采样；默认 rev442 仍为 STOPPED。[资格与原失败记录](evidence/serve-agent-sessions-20261123/profiles.json)

会话的 7 条路径、11 项操作已列入发布用 OpenAPI。创建时的 JSON 或 SSE、事件订阅的 SSE、提交事件的 202 空响应、Beta 请求头和列表元数据筛选都有合同回归。原生入口与可选 nginx 逐路由转发通过。PR #66 首轮 CI 的合同遗漏已修复，等待远端复检。

客户端只有一个数据配额和一个控制配额时，官方 SDK 能在真实模型生成过程中取消回合。此时另一个会话尝试启动推理得到 429；释放配额后以同一个键重试成功，历史中恰好多一个回合。还修复了可选 nginx 监督器拒绝事件通道的 403。10 项 CPU 检查及两种入口的转发均通过。 下一步验证跨主轻配置接续原工具调用。[业务回执](evidence/serve-agent-sessions-20261123/admission.json)

官方 SDK 经原生 TLS 取消等待工具的回合和正在生成的回合，流中各有一个取消终态。迟到的工具结果收到 409，另一客户取消收到 404，重发旧取消键后下一回合正常答 42。9 项 CPU 检查覆盖部分结果留存、恢复前撤销排队工作、会话隔离及原有 Batch 和 Responses 取消。首轮短验因夹具修改了 SDK 不允许更新的字段而返回 400，已保留。 接着验证单数据槽的取消和后续模型工作准入。[回执](evidence/serve-agent-sessions-20261123/cancel.json)

PR #65 的两项 host-contracts 已通过，受检提交 1d57e0897 与主线合并提交 92eef5137 的文件树一致。rev442 注册配置及交付记录已进入主线。[主线回执](evidence/serve-agent-registration-20261123/main.json)

官方 Node SDK 经原生 TLS 修改会话设置，按 Agent ID 与两个元数据键筛选出一条记录。另一客户列表为空、修改返回 404，清除设置后原筛选同样为空。原回合正常答 42。4 项 CPU 用例覆盖跨存储分页、旧回合快照、回填及保留关系，两种入口的实际转发通过。开销配置的存取已验，费用结算另行验收。[会话设置验收](evidence/serve-agent-sessions-20261123/settings.json)

Managed Agent 流式工具短验通过。首次官方 SDK 在函数参数尚未齐备时就按 item.added 调用本地工具，回填得到 409。修复后只在持久化交接成功后向 SDK 公布可调用项。创建流 26 个事件，工具只执行一次；从空闲会话订阅的下一回合 19 个事件，两次均返回 442，未重复调用旧工具。跨客户订阅 404。继续完善会话管理、取消和恢复。[流式验收](evidence/serve-agent-sessions-20261123/stream.json)

官方 SDK 的真实模型经原生 TLS 调用 read_release_record，从本项目发行回执读出 442，并在同一回合给出正确答案。SDK 重发同一结果得到 202；变更载荷 409、其他客户端 404，只保存一份工具结果。计入两步共 876 tokens。并发 CAS 失败后的重复回执还通过 CPU 验证。下一步继续流式、取消与恢复。[工具闭环](evidence/serve-agent-sessions-20261123/tool-round.json)

PR #65 的首轮 CI 发现 Conversation 用例误包含 Agent 配置路由，网关夹具缺少 Agent ID 和上游路径。已校正分类并单独断言 Agent GET、POST、DELETE 与 Conversation 拒绝结果。两项 C++ 合同、可选 nginx 及已封包原生网关的实际转发检查通过，继续等待两项远端 host-contracts。

Managed Agent 初始非流式回合已通过官方 Node SDK 和真实模型短验。SDK 经原生 TLS 提交发行记录，模型答 442，回读 112 个输入和 4 个输出 token、分页消息及持久化回合；跨客户读取和删除后回读均为 404。保留两次夹具失败。rev442 默认仍为 STOPPED。继续事件、工具回填及后续回合。[会话短验](evidence/serve-agent-sessions-20261123/README.md)

标准 Beta Agents 配置已通过官方 Node SDK 的原生 TLS 短验。两个不同客户端验证了配置隔离，跨模型进程重启后仍可读取和修改七类工具配置。五个 HTTP 操作覆盖 157 条范围内路径中的两条。主轻资格已通过，轻配置可在接管主配置后更新和删除原对象。轻资格文件从 8,385,882 降至 5,451,698 字节，可逐字节还原旧资格。rev442 已发布，默认保持 STOPPED。真实托管接管后旧配置、单终态和 Chat 均可使用。24 小时计划完成预检。[发行与交接](evidence/serve-agent-registration-20261123/delivery.json)[验收与当前边界](evidence/serve-agent-registration-20261123/README.md)

PR #64 两项 host-contracts 通过，已合入 main（6f0ffca0f）。合并树与受检提交一致。rev442 注册配置和交付记录见 PR #65。

默认已发布 **rev440，STOPPED**。模型骤停后 SDK 保留原代理输出并收到一个中断终态；网关致命退出后注册回退包接管，续读到旧模型排空前提交的 wait_agent，随后 Chat 答 42。用户 24 小时计划已固定。NVML 冷启动通过原包预检，设备仍由相机与 jtop 持有。[交付回执](evidence/serve-responses-agent-interruption-20261123/delivery.json) 模型步骤排空、RPM、TPM 与非法预约现在各有对应说明。9 项针对性 CPU 检查通过。候选经原生 TLS 和真实模型验证。代理创建子任务后，合法父进程通过配额管理通道发起排空；SDK 收到准确的 service_draining 说明，已提交输出及唯一失败终态可回读，另一客户 404。 当前 rev440 的排空错误码正确、提示仍是旧文案。[排空提示检查](evidence/serve-responses-agent-interruption-20261123/draining-message.json)

主、轻配置各从同一份 rev438 故障原件恢复一次，两次均在首读提交失败终态并保留两项输出，84 次设备采样正常。两份新资格逐层重建旧资格，自动切回有该缺陷的 rev438 已拒绝。待托管主备接管和发行。[主轻资格](evidence/serve-responses-agent-interruption-20261123/profiles.json)

rev438 的后台托管代理在模型退出后卡在 `in_progress`。候选修复已用原故障对象验证：首读返回 `failed`，从断点只收到一个终态事件，已保存的调用和输出一致，另一客户收到 404。8 项 CPU 用例通过；主轻配置及发行继续验收。[故障现场与恢复记录](evidence/serve-responses-agent-interruption-20261123/README.md)

默认 **rev438，STOPPED**。原生维护修复已封包并通过实际入口，rev436 已注册为回退包。PR #62 两项检查通过并合入 main。首笔 WebRTC 在缺少可选组件时得到媒体专属 503，随后 Chat 答 42；历史通用 503 根因待证。[发行与冷启动证据](evidence/serve-native-maintenance-delivery-20261123/README.md)

上一发行已发布至 **rev436，STOPPED**。主网关致命退出后，注册回退包再次完成父子推理，能按身份回读故障前的完整输出。归档已满的发布意图与版本注册时修复了新旧根交接的维护死角，修复后的工具已在实际部署使用。[发行、恢复与长稳入口](evidence/serve-responses-agents-delivery-20261123/README.md)

主轻两套正式配置都已完成父子代理推理、Chat 和 WebRTC。轻量配置按身份读回主配置的代理输出。已修复 rev434 旧资格标签导致的恢复来源拒绝，保留原始失败。继续托管恢复和可回退发行。[主轻验收](evidence/serve-responses-agents-profiles-20261123/README.md)

托管子代理的六种动作均通过官方 SDK 跨轮业务。父代理中断了尚未运行的任务，重新分配、发送新值、列举和等待后，子代理只运行一次并回答 47。40 个事件通过 schema，回读成功且另一客户被拒。下一步验证主轻配置与恢复。[跨轮记录](evidence/serve-responses-hosted-agents-20261123/cross-round.json)

具名多代理历史已随 PR #61 合入 main。托管调度候选已在 Thor 完成真实父子推理。父代理创建并等待 reader，两者均答 37，24 个原生 TLS 流式事件及存储回读通过。首次真实业务暴露的跨轮调用 ID 冲突已修复。下一步覆盖独立轮次的中断、其他动作组合、恢复与可回退发行。[调度验收](evidence/serve-responses-hosted-agents-20261123/README.md) · [历史验收](evidence/serve-responses-agent-history-20261123/README.md)

此前默认 **rev434，STOPPED**。Responses Beta 已封入主包与独立回退包，实际网关致命故障后的模型接管、Beta 与 Chat 历史回读和 WebRTC 均通过。24 小时计划已固定到 rev434，由操作员执行。[发行与长稳入口](evidence/serve-responses-beta-delivery-20261123/README.md)

Live、SDK 默认 Beta、主轻资格、准入修复及 rev434 发行记录均已合入主线（PR #58、#59、#60）。启动期通用 503 仍待定位。[主轻证据](evidence/serve-responses-beta-profiles-20261123/README.md)

rev430 的实际监控响应增加了三个 Live 旁路指标。验收观测器现支持完整新组并保留旧 v5 回读，首次短稳失败保留；40 项 CPU 检查和首周期原始 HTTP 指标重放通过。继续验证正式短稳工具与当前包的配合。

默认部署 **rev430，STOPPED**。Live 主包和独立回退包已注册，默认 CLI 检查通过。实际网关致命故障后，第二代模型接管，官方 SDK 两次 WebRTC、两次 Chat 和跨代存储回读通过。下一步核对主线 CI、定位启动期通用 503，并推进余下接口和恢复组合。[交付与回退证据](evidence/serve-live-default-20261123/README.md)

Live 托管候选已在可选媒体运行库存在和缺失两种环境完成短验。网关恢复后的官方 SDK 会话、Chat 与存储通过；缺 ICE 时已就绪核心继续服务。一次启动初期通用 503 仍待定位。默认 rev428，下一步注册同能力候选并验收回退。[部署与实测](evidence/serve-live-managed-workers-20261123/README.md)

Live 正式主轻资格已封存。两份新记录均可还原旧版完整业务，显式正向迁移通过，跨能力自动回退被拒绝。恢复包 v28 核对实装媒体程序。默认 rev428；继续真实托管候选。[正式资格与兼容边界](evidence/serve-live-registry-20261123/README.md)

Live 最新引擎的两套实际 HTTP 容量、内存准入、工作程序与业务回执已绑定到封包输入，旧验收文件可独立重放。两组正例和九个反例通过，继续封入正式托管资格。[主轻输入](evidence/serve-live-profile-observations-20261123/README.md)

已用 rev428 同一组真实主包、回退包在私有部署触发主网关 SIGABRT。一次设备探测后，第 2 代模型接管并答 42，可读取第 1 代已存回答；正式部署保持停止。[自动版本回退验收](evidence/serve-control-fallback-20261123/README.md)

正式默认部署已发布到 **rev428**，通过默认 CLI 的 check、发布和版本状态检查。保持 STOPPED，回退包已注册；模型、配额和容量沿用原配置。继续正式 Live 主轻配置封包与恢复验收。[正式部署回执](evidence/serve-control-delivery-20261123/README.md)

已修复注册控制包在模型启动前的超时。真实网关故障恢复后模型保持第 1 代，前后 Chat 均答 42，已存回答可取回。67 项 Python、12 项原生检查通过。继续停止状态默认发布及正式 Live 候选。[启动与恢复验收](evidence/serve-registered-startup-20261123/README.md)

真实默认部署的 4 条过期发布请求已移入可恢复归档，现占 124/128；66 项受保护文件摘要一致。控制包首次业务启动另暴露版本日志 prepare 超时，尚未放行模型。继续定位后重验，默认 rev426 保持停止。[维护验收](evidence/serve-model-history-maintenance-20261123/README.md)

STOPPED 发布已支持把模型历史凭据固定在原事务中。48 项 CPU 检查覆盖指针切换中断后的重试、过期目标拒绝和既有回退预算。真实 rev426 的发布前任与回退包已通过只读预检，发布请求目录占用 128/128，继续可恢复归档与真实管理版本交付。[事务验收](evidence/serve-model-history-publication-20261123/README.md)

已用封存的管理工具完整重放停止部署的 12 条版本注册，涉及 12 份历史包，共用同一份已审计的模型资产清单。原始发布、业务凭据、兼容性与注册绑定均通过；普通运行选择继续拒绝过期包。待接入停止状态的发布事务并验证正式 Live 候选的恢复和切换。[历史与验证边界](evidence/serve-model-rollback-history-20261123/README.md)

版本选择器可使用发布决定、实际业务验收、模型内容报告和上游索引四份固定凭据识别 rev426 的独立回退包。主机安装选择器现按 v18 至 v27 各自的源码清单校验。65 项 CPU 检查及真实封包来源复核通过。默认部署仍为停用的原包，下一步处理该状态的事务修复及 Live 正式候选。[注册与主机选择验收](evidence/serve-model-rollback-registration-20261123/README.md)

发行配置 schema 已补齐实际 HTTP 返回的七个顶层字段及追踪启动状态，包括 SIP、TTS、语音注册和追踪健康。两代候选的四份原始主轻响应均通过独立与内嵌 schema。[验证记录](evidence/serve-live-config-schema-20261123/README.md)

rev426 的独立回退包已由真实守护器启动到 READY，经原生网关完成 Chat=42。原包记录中 ctime 变化的 196 个文件已逐个匹配两个固定发布者版本，读取 83,995,035,950 字节。默认入口仍指向停用的原包；继续正式版本注册与切换验收。[模型审计与回退业务](evidence/serve-model-ctime-rollback-20261123/README.md)

认证 `/v1/capabilities` 已修正 Live 和 Realtime 的过期声明。主轻两套实际引擎现在都列出已执行的会话、WebRTC 呼叫、SIP 控制及临时凭据兑换入口。16 项原生能力测试、6 项迁移检查和两套配置的实际 SDK/Chat 均通过。[声明与证据](evidence/serve-live-discovery-20261123/README.md)

主配置与轻量配置已在同一严格 GPU 租约通过 Live 业务短验。两次启动使用实际托管命令和已固定身份的媒体程序；均完成 WebRTC 创建、数据通道开关及 Chat=42。主配置创建的 Conversation 在轻量配置中仅原客户端可读。正式包的守护器恢复和版本回退仍在推进。[业务与边界](evidence/serve-live-primary-light-20261123/README.md)

托管包已支持选配三个安装后的媒体程序，按引擎与发行验收记录绑定 SHA-256。真实 SDK 使用被改变的 WebRTC 程序收到 503，原模型的 Chat 返回 42；放回合格文件后，同一模型上新会话收到 201 并完成数据通道开关。信令与 RTP 的固定身份 CPU 短验也已通过。继续正式主轻组合的封包、恢复和回退。[验证记录](evidence/serve-managed-worker-binding-20261123/README.md)

托管服务可选配 WebRTC 本机地址和 SIP 中继配置。实际引擎在封包时读取私有文件，启动及恢复时对读取到的同一批字节核验 SHA-256。严格 GPU 租约内，替换为另一份语法有效的中继配置后，SIP 报告一次激活失败，同一模型仍通过 Chat 返回 42。六项 CPU 检查覆盖从 CLI 封包、外部文件消失、非法地址到自动回退授权边界。继续媒体程序身份与主轻恢复。[验证记录](evidence/serve-managed-trunk-binding-20261123/)

原生 `q4t service prepare` 已补齐 Live 录制开关，并修复管理工具快照缺少原生来源、无法首次封包的问题。实际 CLI 的首次封包、父包生成子包、启用或省略录制、来源不符拒绝，以及离线回退共 11 项检查通过。继续媒体程序身份绑定、可选中继配置和主轻恢复，默认 rev426。[封包及回退](evidence/serve-recovery-recording-option-20261123/README.md)

WebRTC、SIP 信令与 SIP 媒体已有显式 CMake 安装组件。三个可执行文件迁移至新目录后通过 CPU 的 WebRTC/Opus 和 SIP/SRTP 回环，安装产物不含开发目录搜索路径。下一步将安装后身份和可选中继配置绑定到托管包，验证组合恢复及可回退发行。默认 rev426。[安装与回环证据](evidence/serve-live-worker-install-20261123/README.md)

具名工具的字符串枚举已修复一处可复现的 502 路径。约束解码器允许的参数值两侧空白现在能正确解析，枚举值自身带空白仍可精确区分。12 项 CPU 检查通过；同一电话场景完成实际工具回填和 rev426 播报，挂断后 Chat=42，GPU 已释放。原先那次 502 缺少原始参数，仍保留。继续托管组合与可回退发行，默认 rev426。[复现与业务验收](evidence/serve-tool-enum-padding-20261123/README.md)

Realtime 临时凭据已验证入口与模型的换代边界。入口异常退出后，同一凭据在原模型上可新建会话；撤销一个签发 Key 后，另一客户仍可连接。即使 TLS、客户 Key 和私有绑定都相同，新模型仍拒绝旧凭据，新签发可用。两代普通 Chat 均返回 42。README 的可选 nginx 前置示例已修复 WebSocket 升级，5 项进程回环通过。继续托管组合、旧式 nginx 独立鉴权模式及发行，默认 rev426。[恢复与前置记录](evidence/serve-realtime-client-recovery-20261123/README.md)。

Realtime 临时凭据已通过两个真实 WebRTC 业务场景。对话调用保留文本模态和手动音频设置，按连接覆盖指令并回答 42。仅转录的凭据在独立 Opus 音轨上识别出“请查询当前项目版本号。”，切换为对话类型得到 400。通过轮 47.60 秒、同模型 Chat=42，GPU 已释放。下一步为凭据恢复、可选前置组合及发行，默认 rev426。[配置与媒体证据](evidence/serve-realtime-client-rtc-20261123/README.md)。

Realtime 临时凭据已通过原生 TLS 和官方 SDK 的对话与转录验收。相同客户的长期 Key 与临时凭据共享准入；到期的新连接返回 401，已建立会话继续回答 42。真实录音转录为“请查询当前项目版本号。”。1 次启动，51.89 秒，GPU 已释放。继续 WebRTC 临时凭据、组合恢复及发行，默认 rev426。[签发和兑换证据](evidence/serve-realtime-client-issuer-20261123/README.md)。

Realtime 短期凭据的本机签发与兑换组件、对话及转录类型已通过 7 项原生和 7 项公开合同检查，508 个 SDK 样例零差异。并发签发、客户隔离、到期回收及继续持有已兑换的配置均已验证。公开 HTTP 签发、原生入口兑换和实际客户端业务待接通。[凭据合同](evidence/serve-realtime-client-secret-contract-20261123/README.md)。

已修复两种入口对 Realtime 旁路连接的误限流。持有主 WebSocket 的客户现在还能同时接入两个合法旁路，第三个收到 429。原生与可选 nginx 各保留一条原始失败，4 项进程回环通过。[容量回归](evidence/serve-realtime-sideband-capacity-20261123/README.md)。

Realtime WebRTC 创建与挂断已通过 Thor 本机原生 TLS 和官方 JS SDK 短验。裸 SDP 及带配置的 multipart 均返回 201，所属客户端的旁路与音轨操作同一会话，其他客户得到 404。一通 Opus 语音查询完成工具回填并播报 rev426，挂断后 Chat=42。67.30 秒，GPU 已释放。接下来补临时客户端凭据及组合恢复、发行，默认 rev426。[真实通话和入口回归](evidence/serve-realtime-webrtc-create-20261123/README.md)。

Realtime 电话打断已通过默认对话的真实业务短验。应用手动停播及来电方实际插话后，同一语音项分别保留 985 和 1,702 毫秒，取回字节均为原 PCM 的精确前缀，转录清空。两次挂断后 Chat 正常、GPU 释放。一次 NVML 环境中断和一次命名工具返回 502 均保留；工具调用失败的根因仍需定位。下一步是 Realtime WebRTC 建连、临时凭据和剩余组合交付，默认 rev426。[打断验收](evidence/serve-realtime-call-playout-20261123/README.md)。

Realtime 独立接听和按 `call_id` 接入的官方 SDK 旁路已通过 Thor 原生 TLS 验收。一通加密 Opus 来电完成按键、语音查询、真实工具回填及播报 rev426。迟到的同客户旁路拿到当前会话，其他客户接听、挂断与旁路均得到 404。通过轮 68.48 秒，普通 Chat 回答 42，GPU 已释放。前两轮分别因简繁写法和客户端语音输出预算不足失败。本轮默认发行仍为 rev426。[业务证据](evidence/serve-realtime-sip-accept-20261123/README.md)。

Realtime 拒接、转接的运行时输入和公开合同已对齐官方 SDK 自身类型。492 个 SDK 正反例零差异。一次真实来电验证 `reject(id, null)` 得到远端 603，而 `{status_code:null}` 得到 400，通话保持可处置。37.49 秒，普通 Chat 及清理通过。该轮验证了独立 Realtime 接听的扁平输入。[验收与边界](evidence/serve-realtime-sip-control-contract-20261123/README.md)。

Live SIP 双向电话按键已通过 Thor 原生 TLS 短验。电话发送的 `#` 在 SDK 旁路只出现一次，连续发送的两个 `*` 各自在电话对端解密成功；未协商的按键得到关联错误。按键后同一通话继续查询项目资料并播报 rev426，挂断后 Chat=42。45 项针对性检查通过。短验 64.44 秒，GPU 已释放。下一项为 Realtime 独立接听及按 call ID 接入旁路，默认发行仍为 rev426。[业务验收与原失败](evidence/serve-live-sip-dtmf-20261123/README.md)。

官方 Live 和 Realtime SDK 的电话转接已在 Thor 原生 TLS 上通过。一次模型启动、同一通电话的三次转接分别验证了中继拒绝、受理后目标忙线、受理后目标接通。随后原通话仍完成真实项目资料查询并播报 rev426，挂断和普通 Chat 均正常。63.14 秒，GPU 租约已释放。电话部分继续 DTMF 和 Realtime 独立接听。[转接验收](evidence/serve-live-sip-transfer-20261123/README.md)。

官方 Live SDK 已通过原生 TLS 接听电话。最终候选一次启动完成两通电话：第一通的媒体进程在 ACK 前被暂停，HTTP 返回 503，中继主动挂断并释放资源；第二通由同一模型从加密 Opus 语音识别“请查询当前项目版本号。”，客户端回填真实记录后播报 rev426。主动挂断、旁路终态和普通 Chat=42 均通过，65.96 秒。之前一次与本项目 CPU 夹具重叠的 ENV_ABORT 单独保留。下一步是转接、DTMF 与 Realtime 独立接听。默认 rev426。[验收与回执](evidence/serve-live-sip-accept-20261123/README.md)。

原生电话中继在一通真实本机电话中完成接听应答、远端 ACK、G.711 加密传音和双方挂断。组合回环 24 项通过。主动挂断能区分远端 200、481 和连接丢失。下一步将接听、媒体所有权与 HTTP 客户绑定，验证语音业务。[电话对话记录](evidence/serve-sip-dialog-20261123/README.md)。

独立电话媒体进程已在 Thor 上完成 Opus、PCMU、PCMA 双向加密语音和加密 RTCP 回执。9 项回环检查通过，修复关闭竞态后三种编码器共 24 次排空通过。接下来把安全媒体连接到中继接听、远端 ACK、挂断和应用会话。[验收与原失败](evidence/serve-sip-secure-media-20261123/README.md)。

同一入站电话现可按部署订阅 Live、Realtime 或两种签名通知。两个独立操作 ID 共用最终回执；官方 Realtime SDK 省略拒接参数时，SIP 对端实收 603、ACK 后 HTTP 200，随后使用原 Live ID 得到 404。36.08 秒短验、后续 Chat=42 及资源回收通过。下一项接听、双向安全媒体和已接通电话控制。[真实 SDK 和 CPU 竞争验收](evidence/serve-realtime-sip-reject-20261123/README.md)。

原生 serve 已接入按客户绑定的电话中继。签名回调通过官方 SDK 发出拒接，另一客户得到 404，所属客户在电话对端确认 486 后得到空正文 200。首轮暴露通知进程随启动线程退出的问题，CPU 复现修复后，第二轮 GPU 短验 37.54 秒通过，随后 Chat 返回 42。默认 rev426，后续继续 Realtime 电话控制、接听与安全媒体及组合发行。[实测与原失败](evidence/serve-live-sip-reject-20261123/README.md)。

Live 来电签名 webhook 已通过实际 SIP 和 HTTPS 两个对端。官方 SDK 对真实 POST 验签，307、503、错误 CA、发送进程退出及慢接收端均已覆盖。正式候选的专用进程通过 14 项回环，拒接/ACK 可在通知挂起期间完成。下一步接入 serve 部署与客户 HTTP 操作。[真实验收](evidence/serve-live-sip-webhook-20261123/README.md)。

SIP 私有来电通知已保留原始有序头字段，过滤鉴权字段；SDP 仅可由同身份所有者读取。模拟通知接收端积压时，新来电收到 503，积压前的通话仍能完成拒接和 ACK。9 项回环通过。下一步接入签名业务通知、公开操作及 SRTP。[元数据与背压验收](evidence/serve-live-sip-invite-20261123/README.md)。

独立 SIP 中继已能在双向 TLS 上接收多路来电、校验客户身份、处理取消和拒接，并用远端 ACK 确认最终答复。Thor 上 6 项真实回环检查通过，含同 CA 错误中继和 30 秒超时。后续需将来电通知、HTTP 接听和 SRTP 媒体连到 Live 会话。[信令实测](evidence/serve-live-sip-trunk-20261123/README.md)。

SIP 接听配置已经接入原生会话所有者，HTTP 包装中的 `session.type=live` 校验后才进入通用启动配置。拒接码、转接 URI 和旁路 DTMF 按电话合同校验。15 项针对性 CPU 用例通过，公开电话入口仍待接入真实 SIP 对话与 SRTP 媒体。[验收与待办](evidence/serve-live-sip-contract-20261123/README.md)。

Live 已能从已完成的录制会话创建独立 WebSocket 或 WebRTC 子会话。官方 SDK 通过原生 TLS 实测两种入口，其他客户端均收到 404。子会话逐项继承 9 条前端历史和有效指令，没有重跑原查询，收到新请求后用语音回答 rev426。先前一次只播报 rev，保留质量失败。WebRTC 权限、后续 Chat=42 和资源回收通过；工作分支尚待组合发行。下一项是 SIP 电话接入。[测试记录](evidence/serve-live-fork-http-20261123/README.md)。

显式录制的 Live 会话已在正常关闭时原子保存录音与私有对话状态。一次真实资料查询留下 9 条前端历史、1 对委派调用与结果、关联音频消息的识别文本及最终回答 rev426。首轮驱动误按裸字符串比较转录，保留 FAIL；原件复核及 6 个反例通过。下一步将这些状态接到 WebSocket 和 WebRTC fork 启动路径。[存储与验收](evidence/serve-live-fork-state-20261123/README.md)。

同身份的官方 SDK 已能旁路接入进行中的 Live WebRTC 通话，断开期间主端可继续控制，重连按原 ID 回放最近三秒的事件。修复了旁路关闭时主端可能丢失终态的问题。修复后的短验为 38.89 秒，主端收到终态，旁路正常关闭，随后 Chat 返回 42，资源已回收。另行校正媒体创建成功状态码，官方 SDK 通过原生 TLS 实收 201 Created，46.38 秒。下一项是已存储会话的 fork，其后继续 SIP 和可回退发行。[两轮实测](evidence/serve-live-sideband-20261123/README.md)。

Live WebRTC HTTP 创建已通过真实 SDK、受限前端与项目资料语音闭环。一次 GPU 启动 66.40 秒，原生 TLS 接收 SDP，通话中同客户端再次创建收到 429，模型转录正确、回填后播报 rev426，关闭后普通 Chat=42。已释放客户端名额、主机预算和 GPU 租约。继续同身份旁路、fork 和 SIP 控制。[验收与边界](evidence/serve-live-media-create-20261123/README.md)。

Live 增加 WebRTC 前端事件权限校验及原生 PCM 规划。验证了空名单、精确嵌套 Responses 选择、256 项边界，以及协商格式和公开会话字段的区别。13 项针对性 CPU 检查通过；服务端 HTTP 创建、实际通话的权限执行和旁路仍在接入。[权限与格式验收](evidence/serve-live-media-policy-20261123/README.md)。

修复原生网关与模型对 Live 显式 WebSocket 子协议选择不一致的问题。两个进程使用共同函数选择 `live` 或 `realtime`。8 项 HTTP 检查、9 项实际入口进程检查和 1 项打包来源检查通过。真实模型和原生 TLS 验证了三种 101 握手，Live 正常启动和关闭，Chat=42。单次启动 36.60 秒，已释放 GPU。[握手与验收](evidence/serve-live-subprotocol-20261123/README.md)。

WebRTC 独立媒体进程已在 Thor 上完成回环 ICE、DTLS 证书绑定、SRTP 双向音频和 SCTP 命令验证。Opus、PCMU、PCMA 均在关闭数据通道后继续传音；无插件、错误证书和启动超时均已验证。此阶段还未接入 Live HTTP 创建及模型。[传输实测](evidence/serve-live-webrtc-media-20261123/README.md)。

已补齐语音、追踪、Decisions 和录音的公开协议。90 个 OpenAPI 操作覆盖运行时 91 项路由，转录由同一路径的查询参数选择。原生及可选 nginx 入口均已核对 73 个 HTTP 模板，三种 WebSocket 握手通过可选前置。修复了已存储 Chat 音频与缓存标记的悬空引用。[协议与入口验证](evidence/serve-voice-public-contract-20261123/README.md)。

Live 录制已通过真实 SDK 和项目资料语音闭环。下载 655,107 帧 WAV，双声道逐样本与原始输入和线上输出吻合。发布前及跨客户端均为 404，正常关闭后 Chat=42，GPU、端口和锁已释放。接下来是媒体会话旁路、fork 和原生传输。[真实录音验收](evidence/serve-live-recording-http-20261123/README.md)。

Live 会话的显式录制、隔离存储和同身份 WAV 下载已接到原生入口。16 项原生 CPU 检查、4 项部署配置检查通过，存储写入失败后已有会话继续完成语音。[存储与故障边界](evidence/serve-live-recording-store-20261123/README.md)。

录制链路的双声道 WAV 编排已通过 6 项 CPU 检查，支持 PCM 与 G.711。正在接入持久化和下载。[波形验证](evidence/serve-live-recording-wave-20261123/README.md)。

Live 显式与语音委派、双向转录触发已通过真实 SDK 短验，四次分类和两次语音模型执行共用同一客户端的单个数据名额。项目查询、语音 rev426、正常关闭及后续 Chat=42 均通过。继续可选录制、内容下载、旁路与 fork。[主连接验收](evidence/serve-live-decisions-20261123/README.md)。

原生 `POST /v1/decisions` 已通过真实 OpenAI SDK 多题、正反判断、缓存与计量短验。第二次请求命中 1,454 个共享 tokens，三个请求结算 3,660 tokens，后续 Chat 回答 42。27 项 CPU 检查通过，默认仍为 rev426。[业务证据与原失败](evidence/serve-decisions-http-20261123/README.md)。

Decisions 标准请求与答案合同通过 14 项 CPU 检查，包含工具输入归属、强类型选择、有序评分、单题拒答和真实用量字段。模型取分、HTTP 执行及 Live 自动触发继续。[合同与验证](evidence/serve-decision-plan-cpu-20261123/README.md)。

Live Decisions 已接入会话条目的追加、删除和快照。7 项针对性 CPU 检查通过，实际引擎零警告构建，新增 GPU 启动 0。模型评估和转录触发器待接通。[条目合同与证据](evidence/serve-live-decision-items-cpu-20261123/README.md)。

Live 服务端 Responses 已通过真实原生 TLS/WSS 工具闭环。客户端实际读取项目资料并回填，后台文本和前台语音转录均为 rev426，收到 107,520 字节 PCM，正常关闭后普通 Chat=42。三次查询客户端准入，结算 1,279 tokens。一次独占短验 61.930 秒，63 次 NVML 正常并释放资源。前一轮 NVML 中止的原始记录保留。此前 70 项 CPU 验证仍有效；听感待验，默认发行保持 rev426。继续 Decisions 和其他计划项。
[真实业务验收](evidence/serve-live-responses-http-20261123/README.md) · [CPU 检查](evidence/serve-live-responses-execution-cpu-20261123/README.md)。

Live 委派的事件包装与工具回填通过 13 项 CPU 检查。真实 Responses 输出器的逐项
完成事件决定可回填的调用；程序父调用、MCP 审批 ID 和客户端工具结果分别核对。
提前续接须等工作线程退出，后来出现新调用则需要补结果并再次续接。
正在绑定原生请求执行入口。[续接记录](evidence/serve-live-responses-continuation-cpu-20261123/README.md)。

Live 服务端 Responses 委派配置通过 27 项针对性 CPU 用例。客户端收到的会话
快照移除 MCP 凭据；无效配置在确认后报告并关联原命令，修正后同会话可继续。
旧校验无法放行新设置。实际模型委派、工具回填与语音回传仍待接入和短验。
[配置与验证记录](evidence/serve-live-responses-settings-cpu-20261123/README.md)。

原生 Responses 执行器事件通道通过14项针对性CPU检查，覆盖文本、工具、
用量、背压与错误终态。服务端委派继续接入配置和工具结果续接。
[原始记录](evidence/serve-live-responses-stream-cpu-20261123/README.md)。

[Live音频播放与业务恢复短验通过](evidence/serve-live-fixture-http-20261121/README.md)：
1启动/1片段/4工作/68.022秒，65次NVML正常且资源释放。实际播放逐字节
一致，播放中/显式静音输入未引发额外工作；解除静音后完成正确ASR、
资料读取/回填、语音rev426、正常关闭和Chat=42。原RPM3保持，1717 tokens
结算且预留归零。已有21项CPU与5项驱动反例。累计4启动/14工作/323.305秒，
旧失败保留；听感未验。默认未改，继续Responses/Decisions及全部master。

[Live主WSS客户端真实闭环已验](evidence/serve-live-client-http-20261121/README.md)：
正确ASR、真实资料委派/回填、语音rev426、正常closed/1000及后续Chat=42。
1启动/4工作/64.040秒，原RPM3下无额外ASR，1730 tokens结算/预留归零，
62次NVML正常且清理完成。最后的旧健康断言错误要求ASR同时常驻；
原FAIL保持，5项反例与独立原件复核通过，复核新增GPU0。累计三轮
3启动/10实际工作/255.282秒。继续fixture、服务端委派及其余Live/master
任务；默认rev426保持STOPPED，尚未发行本候选。

主连接及关闭修复已有118项不同原生CPU场景；[主连接绑定](evidence/serve-live-binding-cpu-20261121/README.md)
与[关闭尾部18项针对性验证](evidence/serve-live-close-tail-cpu-20261121/README.md)
通过，实际引擎68bcc25e构建零警告。SDK合同、生命周期、连续输入、
输出/工作线程、调度及客户端上下文的逐组原件见
[Live阶段计划](SERVE_LIVE_2026-11-21.md)。早期夹具/业务失败均保留；
本次只接受主连接客户端资料闭环，未验其他委派、录制/媒体或完整兼容。

[原生Realtime追踪](SERVE_REALTIME_TRACING_2026-11-20.md)已通过
[主轻实际TLS/SDK业务](evidence/serve-realtime-tracing-http-20261121/README.md)：
2模型启动、6实际生成、159.604秒，149次NVML正常，租约与资源清理完成。
两次真实资料读取、引用回答、持久化、独立scope和删除不复活通过；
写入目录失效与追踪子进程退出均有界恢复，健康核心PID不变。
含前两次原始失败累计4启动/9实际生成/260.147秒，原失败保持。
[资格与封包15项CPU闭合](evidence/serve-realtime-tracing-qualification-cpu-20261121/README.md)，
两档完整历史记录可逐字重构，新增追踪见证仍满足8 MiB上限。
原会话身份、空闲重试修复及原夹具失败均保留；模型质量旧FAIL不改写。
主轻资格已签发；六个资格/封包来源经语义复核，22项清单反例通过。
[rev426已可回退交付](evidence/serve-realtime-tracing-delivery-20261121/README.md)：
19条原生命令通过/422.580秒，实际整根回退和再前进、客户状态及预算保持。
当前STOPPED；[PR #57已合入main](evidence/serve-pr57-merged-20261121/README.md)，
合并提交`22669a162`，最终两条检查通过。继续Live及全部任务，
未扩展性能/全模型质量矩阵。

PR #56的CI容量回归及旧版封包清单已修复，
[24项针对性CPU](evidence/serve-realtime-mcp-ci-capacity-20261120/README.md)、
[主轻真实MCP短验](evidence/serve-realtime-mcp-binding-http-20261120/README.md)及
[11项资格边界CPU](evidence/serve-realtime-mcp-binding-gate-20261120/README.md)通过。
含首次驱动漏参失败共3启动/5实际生成/159.322秒，150次NVML正常；
修复后两条资料回答通过，原质量FAIL保持。
[rev416已包含修复并可回退交付](evidence/serve-realtime-mcp-binding-delivery-20261120/README.md)，
19条管理命令通过，350.683秒，客户状态及恢复预算保持、服务STOPPED。
[PR #56已合入main](evidence/serve-pr56-merged-20261120/README.md)，
合并提交`60f5ae908`；两条最终检查通过。未变恢复行为复用原身份证据，
没有重跑。继续原生追踪、Live与全部剩余任务。
[审计清单遗漏已修复](evidence/serve-realtime-mcp-inventory-review-20261120/README.md)：
39个来源及3条Realtime描述经代码/证据复核，20项反例检查通过；0GPU。

[G00范围决定已落实](evidence/serve-scope-decision-20261120/README.md)：
完整推理与应用服务保留，平台管理API按用户决定排除；157条冻结
路径归入实现任务、88条排除，分类不代表兼容通过。总目标继续active。

Realtime MCP的定义、目录、参数流、审批/调用、历史隔离、公开会话
及主轻资格已完成；逐组原始CPU/GPU记录见[阶段计划](SERVE_REALTIME_MCP_2026-11-20.md)。
[实际恢复五组合通过](evidence/serve-realtime-mcp-managed-recovery-20261120/README.md)：
8启动/22工作（含8自检）/9连接/2探测，含原零模型失败共617.179秒，
580次NVML正常且清理完成。实际读取项目资料5次、完成历史认证2次；
TLS恢复保留健康核心，控制器恢复等满配额窗口，整包回退保留能力。
确切默认策略被拒绝的MCP访问不影响普通问答；没有自动重放。

协议/恢复通过，**生成质量仍为FAIL**：配置回退后的一个标题漏掉
“架构笔记”四字，原答案保留，主轻原错答/拒答也保持失败记录。
[Guardian准备子树修复](evidence/serve-mcp-guardian-subtree-cpu-20261120/README.md)
已有7项原生CPU及实际封包准备、命名空间检查；死亡/停止仍完整清理。
新默认候选`17fc1464`及保留包`25ebd972`均实测，资格registry为
`5a44b4a3df68c45cb34f0b0758b51aeb4acd556a98d549db3992a106cb159fa3`。
原TLS证据保留原身份，仅复用未变行为。
[rev406已可回退交付](evidence/serve-realtime-mcp-delivery-20261120/README.md)：
19条管理命令通过，实际整根回退/再前进、客户状态及预算保持，服务
STOPPED。继续main收尾，然后tracing、Live和其他全部任务。

PR #55合入前发现5项旧音频断言与来源标记表示不一致，已修复并完成
[10项针对性CPU验证](evidence/serve-realtime-duplex-ci-boundaries-20261120/README.md)，
运行时未改。代理误将内部网关初始凭据纳入公共恢复证据，当前公共
副本已清理；历史值按已披露处理。[rev396重新签发并可回退交付](evidence/serve-realtime-duplex-credential-reissue-20261120/README.md)
完成，5项认证CPU与19条管理命令通过。当前和自动/手动回退包均已
更换初始值；客户Key、配额、用量与引擎保持，服务STOPPED。原验证
和错误回答均保留，Git历史中的旧值不再作为当前包的初始化凭据。

[rev386双向音频已可回退交付](evidence/serve-realtime-duplex-delivery-20261120/README.md)，
服务保持STOPPED。19条原生CLI完成升级、登记、整根回退和再前进，
新增GPU启动0，客户配额、用量、profile历史与恢复预算保持。主线
已包含[PR #55的完整双向音频与凭据更正](evidence/serve-pr55-merged-20261120/README.md)，
合并提交`abf4dab67`；两条最终检查通过。MCP后续交付见上文；继续
追踪、Live及全部master任务。

同一conversation已接通实际音频输入/ASR、手动commit、VAD自动回答
和打断、空闲、混合及临时音频，组合语音输出、函数工具与持久Prompt。
输入历史、迟到结果、取消用量与清理均绑定实际所有者；不引入额外
外部语音服务。开发细节与原始失败保留在[阶段计划](SERVE_REALTIME_DUPLEX_2026-11-20.md)、
[日志](log/2026-11-20.md)和[逐项任务表](serve-master-goals-v2.json)。

- [取消分类修复](evidence/serve-realtime-duplex-cancel-usage-http-20261120/README.md)：
  实际VAD取消保留文字100/音频336，连同输入与ASR共737 tokens准确结算。
- [主轻资格](evidence/serve-realtime-duplex-composed-profiles-20261120/README.md)：
  确切原业务加当前修复证据组合；主档旧身份来源复用明确记录，未重跑全矩阵。
- [真实恢复](evidence/serve-realtime-duplex-managed-recovery-20261120/README.md)：
  含原失败共6模型/21工作/7连接/1探测、353.339秒，332NVML正常并清理。
  TLS恢复保留健康核心，控制器恢复等满配额窗口，整包回退保留双向能力。
- 原质量FAIL、字段检查失败、最终metrics缺失及命令格式差异均保留，
  不追认原失败；用户允许模型质量与协议/恢复分别记录。未改默认采样，
  未追加性能、完整模型质量或长稳矩阵，听感仍待用户安排。

## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev426；服务包`41760222c76714a23e5753e53ef9e86b3b91e9c0a4f42373f04bf845d3b34afe` |
| 引擎 | `94df51e5baa135d2fe61009d31212191269d9549068de1b78b19590c4e0a717c` |
| 协议 | `03b227b161b55f1ea4e310745226f47aaaca41853b3bb222e95a7b0718cd37ae` |
| 引擎发布记录 | `68d5005b3f1054f6ae785340de90aee86e6505e7722d28734a4f21ff13b9624e` |
| 原生入口 | `8bd79b7a2c998138934541e6e2813d1d778d908cc84c86abec65ba843f954935` |
| 状态 | STOPPED；本阶段没有启动生产服务 |
| 客户限制 | alpha RPM30/TPM6000/输出3457；beta RPM1/TPM6000/输出256；保留动态值与已消费预算 |
| 主轻资格 | `2603a172a402f5a7eb8e2db5d7da897fa84ae3990a26a3afc502ed946951fb71` |
| 保留控制包 | `20871e8a81691446dda42b49429fffff44b12322088f290c5f92333f27870015`；同一追踪引擎/入口和Guardian，只切换已验旧控制器 |

发行完成19条原生管理命令，包括实际升级、STOPPED整根回退和再前进。
追踪采集/持久化/删除及独立故障恢复补了确切新引擎和入口的主轻业务；
未变监督器/TLS实现/后端恢复按原身份证据复用。没有另跑完整运行
故障矩阵，原质量FAIL保留。主轻记录逐字重构完整旧资格，8 MiB上限保持。
旧rev386/rev376和历史发布原件保留；旧披露初始值不得手动复用。

当前Realtime已交付独立转写会话、语义VAD、文字/函数工具对话、
持久Prompt、原生语音输出、同会话双向输入、MCP及原生追踪。Live及其余
矩阵仍是实现任务，不能宣称完整Realtime或完整OpenAI兼容完成。
原答案/模型质量、数值边界、声纹与驱动失败保持各自记录。

## 全部任务进展与剩余缺口

详细内容与原始证据以[任务表](serve-master-goals-v2.json)为准。

| 范围 | 当前状态 |
|---|---|
| G00 | 用户范围决定已落实；完整推理/应用服务必做，平台管理API不纳入 |
| A03—A06/A08 | 采样、工具控制、结构化输出、Completions/Models与持久会话已交付 |
| A01/A02/A07/A09—A12 | 已有大量实际业务交付；剩余字段/媒体/内建后端、Live及完整客户端组合继续 |
| N01—N07 | 原生接入、TLS/CORS/Key/配额、HTTP、运维和发行已交付；nginx/systemd为可选集成 |
| R01—R06/R10 | 辅助故障隔离、自监护和已验恢复已交付；新增能力的恢复组合继续 |
| R07—R09 | 冷准入、物理DRAM边界及压力/宿主组合仍需完成；未获维护交接不干预其他服务 |
| Q01 | 项目资料查询真实闭环已交付，新增后端/客户端/故障组合继续 |
| Q02/Q03 | rev426已可回退交付，追踪PR #57已合main；最终矩阵和长稳交接继续 |

辅助依赖失能不能连带停止健康核心业务；监控恢复不自动重启模型，
真正设备故障仍保留保护。真实云账户验证依赖部署方配置，不能把
本地合同测试写成真实账户验收。长稳工具/交接仍属本任务，实际长跑
由用户开展。

## 工作区、证据与保留候选

有效工作分支仍为`codex/serve-resilience-cd`，已快进对齐PR #57合并
提交；Live合同/配置、生命周期/传输及连续输入已分组提交推送，
输出/工作线程继续形成独立提交，工作分支仍承载Live实现。仅在独有提交、未提交内容、活动PR和证据
路径均核对后整理重复分支，不删除其他任务内容或已发表回退包。

为当前构建恢复了591个host对象，未覆盖后续更新的runtime库；旧
CPU夹具和退役构建库的可逆归档、SHA和恢复脚本保留在
`.q4t-work/serve-realtime-duplex-20261120/`。当前模型与reference目录
保持只读，默认包已在上述验收后显式发布，动态客户状态与预算保持。
磁盘余量不足时另将67个退役CPU测试程序精确压缩归档，释放
188854224字节；当前host测试、runtime和发行包保持，原路径及
逐字节恢复方法见该目录的`retired-host-tests-03/plan.json`与`restore.py`。
本MCP阶段另对14个本阶段退役CPU程序可逆压缩，释放335235523字节；
当前host/runtime/default未改。原路径、SHA和实际恢复核对记录见
`.q4t-work/serve-realtime-mcp-20261120/retired-host-tests-01/`与`-02/`。
其后`-03/`和`-04/`另可逆归档22个退役CPU程序，释放523849368字节，
原SHA、恢复脚本及实际恢复核对保留；当前host/runtime和发行包未改。

另将14个2026-10-31及以前历史包的同字节head副本转为只读共享inode，
保留全部原路径和SHA，释放4362113024字节。原权限/时间、独立恢复
脚本及一次实际恢复/再共享记录见上述输入组合证据；原路径仍可读取，
若需写入须先独立恢复。当前host/runtime/default、模型和reference未改。

Live阶段另可逆压缩37份本任务退役交付快照，释放347293682字节。
原SHA、路径、权限与恢复脚本保留于
`.q4t-work/serve-live-20261121/retired-previous-01/`；当前默认/服务包未动。

视觉行最大值候选及原环境阻断继续保留，见
[视觉补验结果](VISION_ROWMAX_COMPLETION_RESULT_2026-11-07.md)；
本阶段没有追加该候选GPU测试。原研究/回退包与依赖原路径的工作区
继续保留。

早先整理的STATUS原件保存在[2026-11-20历史快照](HISTORY_STATUS_2026-11-20.md)，
SHA256：`af362f9c1ec4e7df087d577c7922e0414e606bf94e57fd6e7f16c1c0aa9c1d85`。更早原件见[2026-11-12快照](HISTORY_STATUS_2026-11-12.md)。
本次精简前的完整状态保留在提交`aafa4a63e`；历史版本与“待合入”
描述不代表当前默认。开发日志按日期追加，见
[当日日志](log/2026-11-21.md)。
