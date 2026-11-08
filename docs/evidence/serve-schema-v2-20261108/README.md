# A05 schema v2 交付证据

2026-11-08。源码 `1022c3fd9938cab524469863b38fc492178ace41`；
[index.json](index.json)逐一记录615份原件的来源、长度和SHA256，
归档后全部重新核对。详细实现见[阶段记录](../../SERVE_SCHEMA_V2_2026-11-08.md)。

丰富JSON Schema、精确数值、增量JSON/工具、真实图片/视频和文本缓存
组合已通过针对性验收并交付。媒体状态缓存仍是A05/A10必做项，
本次接受不代替该项；完整Serve总目标继续。

| 验证 | 结果与边界 |
|---|---|
| A05 C++ CPU | 累计86项不同用例；最终正则26项、模型提示28项受影响检查通过 |
| 新原生二进制 | 7项journal及6项实际HTTP/TLS/Guardian CPU通过；不声称重跑GPU重启 |
| 实际业务 | 含失败累计7启动/19 Chat，驱动462.437秒，严格租约468.496秒，443次NVML全OK |
| 最终媒体 | 原视频方向正确；实际资料工具与图片/JSON回答闭环通过 |
| 最终文本缓存 | 冷/共享/私有三条内容一致；恢复1024及8846 token，新增schema提示计入预算和身份 |
| 清理 | 全部自有模型退出0，worker回收，端口与GPU租约释放 |

四次业务失败分别是两次有限长度字符串超时、单pattern无解前缀和
schema对模型不可见导致错误视频标签；请求、响应、日志与修复检查
全部保留。未提高worker限额、改题目或放宽schema。两次0模型准备
失败（继承租约参数遗漏、端口暂不可用）单列，不伪计模型启动；
后者没有证据证明由TIME_WAIT造成。CPU/发布准备首次失败也保留。

[接受决定](release-01/decision.json)明确证据复用依赖：未改模型计算，
最终ELF复验受影响提示、媒体工具及文本缓存；此前机舱照片结果复用。
这不是完整性能、全模型质量或长期稳定性接受。

| 交付项 | SHA256 |
|---|---|
| 默认引擎 | `46d7bf70b59c811466d1e34e3d51acf5fa9eaee0d6dcae57e67f399afeba699e` |
| 引擎元数据 | `fe70b669cf5c3af7f2946cf787067770b68ef1d2919dbeae3be860d4baa5ab8f` |
| 协议 | `ab8094f62dc9e4e80d23b6785ee1230684c0653af8d2af8df21e22204fa92a8f` |
| 服务包 | `51d90f4cf27001d97c3fbaab9a54fd508be2bea71ba98ee6c36367f6f13ab699` |
| 服务发布元数据 | `b1734181e972d08aa08a70898b6e99610447800b44c3a834274db2489450c46c` |

[安装回执](delivery-01/result.json)：0模型/0Chat，41个Python控制源
相同；JSON reader改变后Guardian/入口重新构建并定向验收。原本机
策略、Key、配额、恢复预算及ledger字节保持，desired=STOPPED，
未装测试证书、未自动启动模型。原A04引擎f8677aaf/元数据09ffec04
与服务包6be35d37/24d1b0dd保留在
`.q4t-work/serve-schema-v2-20261108/delivery-01/rollback/`，
[回退回执](delivery-01/rollback/receipt.json)可核验。

系统libidn2仅在隔离worker的hostname/email格式能力中按需使用；
缺失属于局部依赖不可用，不触发健康模型停止或重启。原生恢复
状态机及CUDA探针的未改证据明确继承，没有把新ELF称为旧ELF。
未归档模型、ELF、缓存、私有Key或测试私钥；本机不可变包保留它们
各自适用的发布内容及元数据。视觉优化候选保持原身份。
