# SRV-11f：应用管理命令错误合同（冻结范围）

2026-11-04。业务可靠性修复；不作性能优化结论。范围为
`tools/project_tasks/runner.py` 和 `backup.py` 的终端入口。
不变更 C++ serve、TLS 网关 API、模型数值、任务重放政策或长期运行计划。

## 输出与处置

错误在 stderr 输出一个 JSON 对象，stdout 不输出成功结果，退出1；
键盘中断退出130。`--help` 退出0；成功与有效非完成状态仍分别为0和2，
`REQUEST_UNKNOWN`、`ARCHIVED`、`ACTIVE` 不自动变为命令异常。
参数错误此前是 argparse 文本/退出2，现在统一为结构化错误/退出1。

`profile=q4t-project-error-v1`，保留 `error` 类别字段，新增固定 `code`、
`category`、`message`、`next_action`、白名单 `operation` 和 `http_status`。
`error` 是规范化类别，不再承诺具体 Python 异常子类名。
`automatic_retry` 恒为false。格式见[JSON Schema](project-task-errors-v1.schema.json)。
机器客户端按 profile/code 分支；新版本可增加code，未知值保存现场并人工检查。
处置提示不是执行授权，也不证明请求未提交；即使401/503/超时也先核对任务。
未知请求/工具仍须带原revision显式resolve，错误处理器不写账本、不重试、不解锁他人任务。

显式区分输入/私有profile、路径权限、任务/永久键/归档/快照额度、
存储满/只读/缺失、锁竞争、幂等冲突、运行时/凭据迁移、计划变更/过期、
快照过期、保留期、未完成保留操作、已封源、认证/服务/传输/TLS和内部错误。
普通完整性合同仍以 `contract_violation` 保守拒绝；意外KeyError/TypeError或
未分类异常为 `internal_error`，不伪造具体原因。SQLite按原生错误码分类，
不匹配异常字符串。原始异常、堆栈、路径、argv、API Key、HTTP正文不进入错误对象。
成功结果及私有证据的现有字段不在本批脱敏范围。

每个子命令只接受自己声明的参数；拒绝缺少必填项、无关参数、重复参数、
缩写参数及未知参数，先于读取profile或任务库变更。CLI内部Python调用仍用
ValueError子类保留诊断文字；直接导入的调用方须自行处理私有异常。

## 验收边界

先一组CPU：既有88应用合同（含23次文件操作SIGKILL）与新增管理入口/真实
SQLite/实际flock/隐私canary、未知执行状态、注册旧源码升级检查。
冻结源码/日志，失败保留，只重验受影响项。旧五代证据离线复用。

再一次短验：1模型、1TLS网关，同一后端GPU租约；alpha512/beta64各2次
生成、1次真实项目资料工具，共4成功、2工具，答案及实际引用核对。
最多8个负例：无效输入、认证、幂等、绑定、容量、计划错误；重复提交/恢复
不得多生成。上限64控制调用、总480秒、GPU420秒；无长验，不启动day。
验证结束停止自有服务、封存日志、确认GPU/原锁释放及SOAR仍masked。
不填写实际LAN/DNS/CIDR/CA，不将loopback短验称为内网部署验收。

发布以实现提交、CPU证据、真实短验结果和独立重建报告为准。
