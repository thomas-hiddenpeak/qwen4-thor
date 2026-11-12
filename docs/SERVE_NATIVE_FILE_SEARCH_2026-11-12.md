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
| A11.2 | 持久索引与Files版本/归属，原生分块/embedding，事务和重启；CPU存储及实际短验 | 待接线 |
| A11.3 | 向量存储完整入口、file_batches/搜索/属性/分页/过期；原生入口和SDK | 待实现 |
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
