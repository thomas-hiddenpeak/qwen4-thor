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
