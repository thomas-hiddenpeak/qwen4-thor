# 正式服务包中的原生保留管理

2026-11-23，源码中的发布和注册入口在记录池满时返回 `maintenance_capacity_reached`、池名和 `maintenance-status`。已发布新包但尚未注册时，可在 STOPPED 状态按最后一次稳定发布的直接前序核实旧周期并释放槽位。旧包可用项目中已验证的维护入口处理这次交接。[rev436 实际记录](evidence/serve-responses-agents-delivery-20261123/README.md)

N07重新打开的缺口是：rev260有安全归档源码，但密封发行和原生
service入口没有这项能力。本组补齐正式入口，不改模型/协议/推理
程序、容量或恢复策略，不新增外部服务依赖。

## 实施合同

- 新服务包v20增加三类归档模块和统一维护入口；冻结v19来源集合，
  新核验器继续支持历史v1—v19包，不能让新增来源改变旧包身份。
- `q4t service maintenance-status`查询三类池的容量、占用、受保护和
  可候选记录；候选不是免校验授权，prepare/apply再次核对全部引用。
- `maintenance-plan --kind intents|generations|versions --select ID`
  显式选择1—4项，生成绑定状态/原始字节/工具身份的计划；
  `maintenance-apply`及`maintenance-restore`使用该计划和SHA256。
- 操作保持原STOPPED/共享GPU锁/publication/deployment/Guardian
  保护；在持锁期间核对CLI的包、摘要、状态路径和inode，拒绝错包、
  错状态和计划跨服务使用。查询也不启动模型。
- 当前/history/pre-journal/版本迁移和活动文件引用继续受保护；
  同文件系统rename与fsync，中断续做和恢复均不返还恢复预算。
- 旧来源绑定的归档计划与原维护源码快照保留。对已审查、格式和移动/
  恢复语义未变的原版及身份钩子版，显式登记精确来源哈希；新入口可
  续做/恢复原计划。不得修改或重签名旧计划，未知来源仍拒绝。

## 冻结验证与交付

先成组实现。CPU针对新增命令、三类实际文件归档/恢复、错状态/
包/摘要、竞争、状态/预算保持、迁移后的隔离导入及历史包核验。
上限16项新用例及12项直接受影响的现有回归，600秒；具体名单随
实现固定。必要构建/语法/来源检查另列，不执行模型或性能矩阵。
冻结精确新包后，用该包原生入口执行STOPPED查询和保护拒绝，
必要的实际归档/恢复只选已脱离历史的记录，保留原件与回执。

发布前核对完整发布/回退/再前进所需的三个池槽位；先安全归档
足够的退休项，不提高容量，不删除包/证据。原引擎和所有本机helper
未变的业务证据直接复用。实际交付保持原核心/client/outer预算和
当前周期，检查整根回退与再前进，再固定用户day交接（不运行）。
PR43精确head检查/合并独立推进；新阶段不漂移该PR的候选身份。

本组完成后继续A01/A02/A07/A09—A12/R07—R09及G00、Q项，
不会把维护入口交付当总计划完成。

## 已交付入口

rev266已实际发布，[15项发布/回退/再前进记录](evidence/serve-native-maintenance-delivery-20261117/README.md)
包含原rev260入口check和version-status。以下命令由默认包提供；
先停止并确认本服务，维护命令会自行核对持久STOPPED及全部锁。

```bash
./build/q4t service maintenance-status --state build/service-native-state
./build/q4t service maintenance-plan --state build/service-native-state \
  --kind intents --select <旧请求UUID> --output .q4t-work/<已有私有目录>/archive
./build/q4t service maintenance-apply --state build/service-native-state \
  --kind intents --plan <返回的plan绝对路径> --plan-sha256 <返回的SHA256>
# 恢复使用同一plan与SHA256，把maintenance-apply改为maintenance-restore。
```

`--kind generations`选择目录hash，`--kind versions`选择恢复周期UUID；
重复`--select`最多4项。status候选仍须经过活动引用检查，不能据候选
列表直接删除文件。归档后publish-resume先需恢复对应意图；恢复不能
绕过原发布revision检查。历史完整包保留自己的入口与来源核验。

具体CPU清单为`test_maintenance`12项、新v20来源集合1项；旧回归
12项：包默认/冻结旧集合/历史v1—v19核验/文件协议/HTTP合同/
outer隔离导入6项，三类归档各取当前与待恢复保护、rename后中断
续做2项。合计25项，不重复全量模型或服务测试。

精确v20包已通过7项原生命令检查（58.484秒）：包与历史v19状态、
三池查询/保护拒绝、错包拒绝，全部状态字节不变。实际容量为意图
126/128、目录32/32、版本16/16。追加冻结16条维护命令/600秒：
三类各选择1项plan/apply/restore，再以新计划归档4条意图、2个目录、
2个版本登记，最后查询；选择只来自受保护集合之外且活动引用检查
通过的记录。原件可恢复，0模型动作，当前预算/周期不变。随后仍须
实际发布/回退/再前进，不能把归档验证当作发行完成。

16条实际维护已完成，原驱动一次断言把预期意图列表变短误判为状态
变化；原失败保留，仅续做剩余14条。当前意图122/128、目录30/32、
版本14/16，预算和周期保持。新发现旧计划来源兼容缺口：补3项真实
旧代码生成计划的CPU用例，覆盖两代来源；重验3项当前往返及未知
来源拒绝，共7项/180秒。新精确包再恢复历史计划并重新安全归档，
最多9条命令/600秒；原计划/回执不改，不能仅靠旧源码存在声称可恢复。

历史计划的最终精确包另含1项包核验及1项最后容量查询，共11条
原生命令119.901秒通过。正式交付冻结15条命令/900秒、0模型动作：
当前与候选检查、发布/登记、整根回退及旧入口check/version-status、
再前进、默认check/status/observe、profiles/outer/publication和原生
维护查询；同时核对原周期和core/client/outer/profile字节保持。
