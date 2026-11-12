# 原生资料检索与服务端工具执行

2026-11-12，A11/A07/A09/A12/Q01实施计划。当前默认rev80的Files、
专用embedding和Responses对象可复用，但没有向量存储API、索引执行
及内建file_search循环；输入/输出形状或include投影不能替代它们。
本组是总计划的实施边界，完成后继续MCP及其他内建工具和剩余任务。

## 官方合同与实现选择

按OpenAI Docs实际抓取的[file_search说明](https://developers.openai.com/api/docs/guides/tools-file-search)、
[检索说明](https://developers.openai.com/api/docs/guides/retrieval)、
[Search参考](https://developers.openai.com/api/reference/resources/vector_stores/methods/search)及
[添加文件参考](https://developers.openai.com/api/reference/resources/vector_stores/subresources/files/methods/create)
冻结本组协议。参考页的纯Markdown只返回页名，字段以实际HTML参考
核对，不能将缺正文的抓取当合同证据；接线时再核对固定SDK类型。

1. 原生`/v1/vector_stores`及files/file_batches/search接既有Files和
   对象worker。文档由客户端显式上传；filename仅作元数据，不作为
   服务器磁盘路径。所有操作复用已认证scope，文件属性不是权限凭证。
2. 原生Qwen3-Embedding-0.6B产生段落与查询向量；绑定模型/权重/
   tokenizer/维度身份。禁止用生成模型伪嵌入、零向量或外部服务替代。
   新检索能力不要求第三方向量数据库；缺embedding只影响相关操作。
3. `auto/static`分块使用实际token数量；默认800/400，静态大小100—
   4096且重叠不超过一半。原文、UTF-8边界和引用来源可核验；切块
   不能损坏字符或将原文中的工具指令提升为system/developer权限。
4. 属性16项、key64字符、string512字符；比较及集合/组合过滤完整
   执行，数值使用已有精确十进制比较。缺属性不满足ne/nin等比较，
   类型不会自动互转。scope授权先于加载，属性过滤先于排序。
5. 查询为string/array、默认最多10结果且可配置1—50；显式rewrite
   必须有真实模型改写及实际token计量，不能忽略true或返回假改写。
   `none`执行原生cosine；`auto/default-2024-11-15`执行原生cosine与
   BM25的RRF融合，k=60、按理论最大得分归一化。score是本地排序值，
   不声称复制OpenAI私有模型或概率。身份并列按file/chunk稳定排序。
6. Responses把内建检索声明映射为模型可选择的内部调用。真实模型
   选工具后执行检索、回填结果并继续生成；尊重tool_choice、allowed
   tools、max_tool_calls、输出token/时间/内存预算。不得把一次客户端
   function_call及示例脚本冒充服务端循环。
7. 输出包含真实file_search_call、in_progress/searching/completed
   事件及稳定item/output_index；results只按include显露。最终文字
   引用必须绑定已检索文件，不能从模型编造的ID伪造file_citation。
   JSON/SSE、存储/重放/history、后台取消、SDK都接同一执行状态。

## 连续实施与验收边界

| 子项 | 实现和必要验证 | 当前状态 |
|---|---|---|
| A11.1 | 参数、精确过滤、原生有界排序、取消/损坏保护；CPU原语 | 16项CPU首过，基础层已验 |
| A11.2 | 持久索引与Files版本/归属，原生分块/embedding，事务和重启；CPU存储及实际短验 | 索引11项+持久对象19项CPU已验，真实执行器待接线 |
| A11.3 | 向量存储完整入口、file_batches/搜索/属性/分页/过期；原生入口和SDK | 管理/内容15条操作CPU+双SDK已验；search继续实施 |
| A11.4 | Responses真实内建工具循环、引用/事件/预算/失败、历史与后台 | 待实现 |
| A11.5 | 真实项目文档/发行记录查询、双scope及取消恢复，封包可回退发行 | 待实现 |

[基础组16项CPU首过](evidence/serve-vector-search-foundation-20261112/README.md)，
0.026秒、来源/协议检查通过，零警告构建。只改独立原语，未增加
生产路由或能力声明；立即继续持久索引与执行接线。
不因第一组通过标记A11完成。MCP、web search等其他内建工具及高级
caller/async/namespace/output_schema继续是总目标任务，未移出范围。

当前资源界限：最多16查询/合计64KiB，4096候选块、维度4096、合计
8M个float和16MiB原文/元数据、1M稀疏词项；过滤16层/256节点、
64KiB操作数字节及每集合128项。
超出工作预算报资源错误，不截断候选后宣称搜索成功；取消/超时/索引
损坏不发布部分结果。资源上限是原语边界，后续持久容量与并发准入
另行绑定，不能用该表冒充整个服务的RAM硬上限。

成组完成后仅验证直接影响行为。已有embedding数值及真实检索证据
身份不变时复用；新索引/模型工具循环做必要HTTP/SDK短验，GPU预算
在接线完成后冻结并持有同一严格租约。不跑五档性能或整模型质量。
默认rev80保持，未部署未验收候选。

## 不可变索引实现组

2026-11-12。实际raw token边界分块已编码，UTF-8交界向内调整，不
破坏字符、不超过声明的token/重叠；每块实际embedding回调追加单个
EOS。中途失败/异常保留已消费计量，不发布部分索引。权重、tokenizer、
LAST/L2/MRL维度及分块版本形成绑定，文件/提取文本身份同时保留。

持久化复用已有InputSnapshotStore：原文和little-endian float32两段
二进制、不可变内容、哈希/客户端scope/配额、有限准备hold；未知提交
保留原ID和hold，不重复发布或猜测删除。可变文件属性在读取投影时
绑定，不因改标签重新运行embedding。尚未创建公开向量存储入口。

冻结9项索引CPU（含真实已安装tokenizer、实际快照worker重启和故意
丢提交回执）+2项因常量共用而直接受影响的embedding资产CPU；无GPU。
这些用例中的向量由明确的数学夹具提供，不冒充真实模型生成证据。
验收后继续持久父对象/任务管理、HTTP/SDK和Responses真实执行。

[索引11项CPU首过](evidence/serve-vector-index-cpu-20261112/README.md)，
0.984秒，实际tokenizer、对象worker重启及丢提交回执均执行。两次
构建失败及一次未执行启动记录保留；修复头文件/测试入口后零警告。
源文件身份字段仍须由后续Files真实提取适配器赋值；公开父对象、
任务恢复、API与工具循环未完成，继续实施，默认rev80保持。


## 持久父对象与任务所有权实现组

2026-11-12。本组复用现有对象worker、事务、scope与快照，不新增
外部向量数据库。向量库和每个文件关联分开存储，父对象最多10000
关联；对象/字节配额仍共同生效。创建时至多500个Files ID先全部
校验，再原子提交父对象与关联。文件实际提取/embedding执行器和
file_batches入口继续是后续实现项，当前不新增公开路由。

过期策略为last_active_at起1—365天；元数据保留，关联在到期后
逻辑不可用并允许回收。管理读取不延长保留；实际检索/工具使用才
更新活动时间。到期后的关联可能已回收，不能靠修改策略复活旧索引。
关联删除不删除原Files文件；新读取仍检查源文件存在，旧任务必须
匹配随机关联身份才能提交，删除再挂载不能借同一个file_id越权。

任务有独立claim、owner、执行期限与最多3次尝试。超时不自动夺取
运行中任务，原执行者确已退出/清理后才可恢复；总任务期限24小时
不因重启重置。真实恢复控制器仍待接线，当前提供持久状态操作及
有界私有扫描。完成状态仅在索引真实读取验证及父/关联事务确认后
发布；未知提交保留原ID，不重放或擅自释放准备hold。

冻结12项新CPU与6项直接受影响回归（18项）：真实对象worker、Files
上传、并发/配额、索引保留及重启、删除/重挂载、源删除、恢复预算、
真实提交后丢回执、到期依赖回收、跨scope/错误索引和取消/损坏隔离。
向量使用明确数学夹具；0GPU，不将这些结果称作真实模型检索验收。
首构建缺ObjectReply显式字段初始化导致-Werror，已修正并保留记录。


[持久对象19项CPU通过](evidence/serve-vector-stores-cpu-20261112/README.md)。
原失败与修复保留，只重验受影响项。复核新增排队任务总期限/重试
耗尽的原子失败闭合，避免永久in_progress；运行中超时仍不抢占。
官方过期语义参考[Retrieval](https://developers.openai.com/api/docs/guides/retrieval)，
创建/更新字段分别核对[Create](https://developers.openai.com/api/reference/resources/vector_stores/methods/create)
及[Update](https://developers.openai.com/api/reference/resources/vector_stores/methods/update)。
本组默认与公开路由保持；接下来继续真实执行适配和API闭环。


## 真实文件与tokenizer接线组

2026-11-12。准备路径读取scope内真实Files字节，复用签名分类和隔离
提取器；源文件SHA始终来自实际读取。原生tokenizer使用raw encode
再按真实token逐个decode形成规范文本与边界，文本SHA绑定该NFC
结果，原始上传内容仍在Files可核验。分块后所有重叠token和逐块EOS
先算完整预算，配额回调通过后才允许执行embedding。

普通文件输入的表格前1000行窗口保持；向量入库要求完整行，包含
嵌套文件在内超窗口则报告容量不足，不发布截断索引。没有可提取
文字的文件失败，不用零向量/编造摘要代替。PDF页图像不作为本次
文本embedding输入；视觉/媒体检索及后端组合仍按总计划补齐。

执行适配复用实际索引构建、快照和父对象提交。错误/取消仍返回
真实已消费token；不自动重启模型。未知快照或父提交保留原引用和
hold，可原ID重读后完成确认，不再次运行模型。已执行结果不能被
重复调用静默清零。明确成功后的hold清理失败只记辅助诊断。

冻结8项新CPU及3项提取/分块直接回归（11项），使用真实Files、
隔离文档worker与实际安装tokenizer，向量仍为明确CPU数学夹具。
原生HTTP、真实embedding回调/配额归属、持久控制器、file_batches、
SDK与Responses循环继续接线；此适配器不等于已上线工具能力。


[真实文件适配11项CPU首过](evidence/serve-vector-ingestion-cpu-20261112/README.md)，
1.184秒；实际Files/文档worker/tokenizer与持久状态执行，向量仍为
数学夹具。默认rev80保持。继续真实模型/配额与持久控制器接入，
以及HTTP/file_batches/search/SDK/Responses闭环和全部剩余任务。


## 原生embedding与客户端配额适配组

2026-11-12。持久入库任务的内部input reservation使用同一个在线TPM
池、当前身份与并发许可，不能由HTTP字段选择，不重复计算外部RPM。
发布任务的公开操作仍负责自身RPM；该入口须随HTTP接线实现。真实
模型适配沿用已有BF16最终行到FP32 L2归一化，不改变数值算法。

每块实际消费通过已有ActualUsage累计；异常也计量并退还未用额度。
执行者回执不合法时用量标unknown，配额保留整个已准入输入额度，
不把未知当0。取消/存储错误不重启模型。原生ChatServer的实际GPU
回调与持久调度器还未接入，此组先验证私有执行适配和真实配额池。

冻结6项新CPU与8项直接回归（14项）：真实Files/对象worker、原生
BF16数学夹具、配额/并发权限、TPM共享/外部RPM不重复、异常及未知
用量、旧在线/Batch/恢复窗口合同。0GPU，不扩完整正确性或性能。

[配额适配14项CPU通过](evidence/serve-vector-quota-cpu-20261112/README.md)，首轮夹具失败及4项定向修复记录保留，未放宽生产策略校验。0GPU，继续实际持久调度器、原生模型接线与公开API/工具闭环。


## 持久调度与真实原生回调接线组

2026-11-12。持久关联本身作为任务队列；有界扫描和客户端轮换，
默认1执行者/8所有权槽/每客户端4槽，队列不持有提取文本或向量。
每次claim至多600秒、总任务24小时/最多3次，TPM/并发等待不增加
尝试次数。只有原进程身份确定退出才允许恢复，监控未知/期限已过
不允许夺取活跃执行；真实执行失败不会在同一claim中重放。

取消意图先持久化，实际执行清理后才终态；回执丢失保留claim和原
快照确认，不再次embedding。所有终态写入使用独立短存储期限，
辅助线程/存储失败不触发模型重启，不成为核心readiness依赖。
原生ChatServer接入实际tokenizer和Qwen3 embedding回调、同一公平
模型槽、进度与故障保护；默认不加载第二个模型直到有真实入库任务。

冻结14项新CPU与4项直接对象/恢复回归，真实Files/worker、真实子
进程退出身份、提交后丢回执和并发调度；向量为明确BF16数学夹具。
构建实际引擎但本组不启动GPU。公开API、file_batches/search、SDK、
Responses工具执行/引用/事件和真实业务验收仍是下一组必做工作。

[持久调度18项CPU首过](evidence/serve-vector-jobs-cpu-20261112/README.md)，实际引擎零警告构建；0GPU、默认rev80保持。继续公开API/file_batches/search、SDK与Responses工具业务，不以本阶段结束总目标。


## 文件批次与删除清理实现组

2026-11-12。[官方创建合同](https://developers.openai.com/api/reference/resources/vector_stores/subresources/file_batches/methods/create)
与已安装Python3.42.1/JS7.47.0一致给出最多2000项、file_ids/files互斥；
files形式逐文件属性/分块覆盖，忽略全局值。公开object采用两SDK实际
Literal的vector_store.files_batch；官方网页示例单数file_batch差异
已记录在合同冻结文件，不把两份源误写为完全一致。

批次/成员/父库一次事务提交，完成计数与文件属性随成员变更原子
更新。取消先持久化全部意图，仍在执行的任务清理后才变终态。
删除运行中文件立即退出检索；批次保留其私有执行记录直至清理或
原进程确定退出，随后释放引用。旧执行不影响同file_id的新关联。
批次历史仅保留计数/元数据，不永久钉住已删除索引或源文件。

冻结9项新增CPU与8项直接回归：真实Files/worker、批次合同和原子
计数、取消/删除/重挂载、真实子进程退出恢复及提交后丢回执。
向量为明确CPU数学夹具，0GPU；公开HTTP/SDK和实际模型业务待后续
接线并统一短验。本组并不宣称公开向量API已发行。

[文件批次17项CPU首过](evidence/serve-vector-batches-cpu-20261112/README.md)，原构建失败保留，最终零警告；0GPU/默认rev80保持。公开HTTP、搜索、SDK、Responses与真实业务继续实施。


## 公开管理与源内容读取接线组

2026-11-12。在原生后端及原生入口接15条vector_stores管理操作：库、
files、file_batches及实际索引内容读取。双方共用精确路径/方法分类，
保留状态/删除的观察容量和取消专用容量；列表/内容/变更仍用业务容量
与实际客户端RPM，内部入库只记原先TPM。未配置embedding时不接受
无法执行的新入库，持久任务不因worker暂时失能而丢失；元数据与清理
可继续使用。索引读取核对当前归属、原文件存在、不可变身份及真实字节，
变化冲突不混合返回，重复overlap不作为完整原文。

冻结10项新增CPU：精确路径/独立准入、CRUD重启及双向分页、scope与
身份、严格输入合同、批次创建/取消、单外部RPM+真实内部计量、缺配置
时管理仍可用、真实索引内容/属性/重挂载、源删除/取消/期限、索引身份
损坏。另复用既有证据并仅重验两项直接影响的原批次取消与内部配额。
必要host/引擎/原生入口构建，0GPU。随后接实际search与SDK/Responses
循环；本组不是A11或总目标的完成点，当前默认rev80保持。

补充同组交付合同：15条公开操作同时纳入统一application schema和
OpenAPI，锁定现有官方JavaScript 7.47.0的三份Vector Stores源文件。
在同一CPU链追加3项有界检查：官方JS直连、官方Python经原生TLS与
路径前缀、源身份及schema反例；实际磁盘/隔离提取/持久执行，embedding
明确使用CPU数学夹具，不声称真实模型检索质量。每SDK最多100请求、
45秒；只启动临时CPU后端/原生入口，0模型启动、不改默认服务。
首轮12项中11通过；唯一失败因夹具原始库中无关入库尚在队列，已删除
该关联，仅重验受影响的取消用例，原失败记录保留。

[本组12项C++和3项SDK/协议检查通过](evidence/serve-vector-api-cpu-20261112/README.md)。
JS 7.47.0直连、Python 3.42.1经原生TLS均实际覆盖15条管理操作并逐
响应通过统一schema；向量为CPU数学夹具。原失败、正确CMake目标
q4t_ingress补构建及驱动httpx2修复均保留。默认rev80不变，继续
search真实query执行与Responses内建循环，不将本组当A11完成。

统一产物核对另外发现既有Chat JSON/SSE的refusal类型镜像未同步；
已按原生成器补齐，2项复用既有真实响应的针对检查通过，原负控假设
错误及修正保留。本组总17项独立检查，不重跑模型。


## 查询执行与快照一致性实现组

2026-11-12。公开search接线前补齐共用执行器：当前scope、完成成员、
过滤和完整索引字节先核验；实际查询指令/tokenizer加单个EOS，全部
查询成本先准入，真实BF16输出归一化再排序。rewrite_query=true使用
主模型约束JSON，数量/空值/截断/错误不可当成功；原文仅作user数据。
查询输出只含真实文件/段落，模型异常和取消仍保留已消费及未知计量。

私有search_revision仅在成员/属性/索引变化时前进，旧对象可渐进接入；
普通管理元数据和活动时间不冲掉并发查询。最终核验源存在及原快照，
变更返回冲突且不重跑模型；完整候选超界报资源错误，不截断假成功。

本组冻结15项新增针对性CPU及3项直接内容读取回归，实际Files/
对象worker、并发活动CAS与旧记录迁移；向量是明确CPU BF16夹具。
必要构建、来源/合同检查，0GPU、不动默认rev80。之后继续原生HTTP/
双SDK、Responses真实循环及必要模型业务，本组不结束A11或总计划。

[查询执行18项CPU通过](evidence/serve-vector-query-cpu-20261112/README.md)，首轮17/18及夹具TTL修复记录保留；实际引擎零警告，0GPU/默认rev80保持。下一步立即接公开search/官方SDK与Responses，全部任务继续。
