# 持久项目查询任务

这是受控本地应用入口，使用 TLS 网关和一个客户端的独立 API Key。
工具仅查询本项目允许范围内的真实文档、发布和验收记录。
[合同](../../docs/SERVE_PROJECT_TASKS_2026-11-04.md) ·
[本机真实恢复验收](../../docs/SERVE_PROJECT_TASKS_RESULT_2026-11-04.md)。
[双客户端预算与恢复预检验收](../../docs/SERVE_PROJECT_TASK_BUDGET_RESULT_2026-11-04.md)。
[源码升级与同客户端Key轮换验收](../../docs/SERVE_TASK_MIGRATION_RESULT_2026-11-04.md)。
[终态归档、永久保键与中断恢复验收](../../docs/SERVE_TASK_ARCHIVE_RESULT_2026-11-04.md)。
[一致性备份与单活恢复交接验收](../../docs/SERVE_TASK_BACKUP_RESULT_2026-11-04.md)。
[管理错误合同与真实短验](../../docs/SERVE_TASK_ERRORS_RESULT_2026-11-04.md)。

先显式创建新 store，任务不会自动删除，幂等键一直保留：

```bash
python3 -B tools/project_tasks/runner.py init \
  --root .q4t-work/project-tasks --max-tasks 16
```

配置文件须位于 build/ 或 .q4t-work/，权限0600。由可信操作员填写
真实连接 IP、证书 DNS 名、CA 和私有 API Key；下面是结构示例，不是
已部署配置。协议和模板 SHA 从已经验收的发布元数据选取。

```json
{
  "client_id": "alpha",
  "base_url": "https://q4t.internal.example:8443/v1",
  "connect_host": "192.168.1.10",
  "ca_file": "/absolute/path/to/ca.pem",
  "api_key": "API_KEY_FROM_PRIVATE_CONFIGURATION",
  "model": "qwen3.8-flash-next",
  "protocol_sha256": "64 lowercase hex characters from accepted release",
  "template_sha256": "64 lowercase hex characters from accepted release"
}
```

提交一个有业务意义且稳定的幂等键。相同client/key与相同请求合同会
返回已有状态；改变问题或预算却复用同一键会拒绝。这里的client_id
是私有配置，不是可供远端调用者自行冒充身份的HTTP参数。

```bash
python3 -B tools/project_tasks/runner.py submit \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha.json \
  --key release-question-001 \
  --question '读取 docs/MODEL.md 第1到24行，回答 architectures 并引用来源。' \
  --max-requests 4 --max-tools 8 --max-tokens 512
```

任务完成会返回答案和outcome；全部读取失败是UNVERIFIED，不是已核实
的业务答案。submit/resume退出0表示任务流程完成，退出2表示需要继续或处置，退出1
表示拒绝/异常。状态和outcome都应检查，不能只看进程退出码。

管理错误遵循[错误合同](../../docs/SERVE_TASK_ERRORS_2026-11-04.md)与
[JSON Schema](../../docs/project-task-errors-v1.schema.json)：stderr单个JSON，
`profile=q4t-project-error-v1`，按`code`和`next_action`处理；不输出原始
异常/argv/HTTP正文。`error`字段为规范化类别，`automatic_retry=false`。
参数错误也退出1（原argparse退出2），键盘中断退出130。缺少、重复、
缩写或不适用参数在读取profile前拒绝；不再静默忽略`--reason`等参数。

| 常见code | 处置方向 |
|---|---|
| `invalid_arguments` / `invalid_profile` | 修正命令或私有配置 |
| `busy` | 核对持有者；退出后重新检查，不自动重放 |
| `task_capacity` / `key_capacity` / `archive_capacity` | 核对相应容量与保留政策；永久键不自动删除 |
| `plan_changed` / `plan_stale` | 核对摘要/目标及当前revision，再显式重做计划 |
| `retention_pending` / `store_retired` | 核对未完成操作或交接计划 |
| `remote_auth` / `transport_error` / `internal_error` | 先检查任务持久状态，再处理认证/连接或保留现场 |

错误码不会证明一次调用没有执行。`REQUEST_UNKNOWN`/`TOOL_UNKNOWN`
仍是有效任务状态，必须按原revision显式resolve，不能仅因异常而重试。


```bash
python3 -B tools/project_tasks/runner.py inspect \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha.json \
  --key release-question-001
python3 -B tools/project_tasks/runner.py resume \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha.json \
  --key release-question-001
```

resume复用已保存结果和响应，只执行未开始的调用。ACTIVE表示另一个
应用进程持有任务锁；REQUEST_UNKNOWN/TOOL_UNKNOWN不会被自动执行。
所有请求/工具尝试和运行次数继续消耗原预算。

`--max-tokens`只用于submit，范围1..512，默认512。按客户端策略选取，
例如beta策略上限64时显式传64；同一任务的预算在resume时不能覆盖。
每轮先以完整对话进行只读token/客户端预算预检，不接受静默缩短输出。
PREFLIGHT_REJECTED保存有界错误分类、code和param，不消耗生成/工具次数。
修正外部条件后可显式resume；PREFLIGHT_PENDING/PREFLIGHT_APPROVED也会
重新预检，旧批准不代表资源预留。新任务实现见
[预算合同](../../docs/SERVE_PROJECT_TASK_BUDGET_2026-11-04.md)。

未知结果需先inspect，核对原记录，然后携带它返回的完整revision：

```bash
python3 -B tools/project_tasks/runner.py resolve \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha.json \
  --key release-question-001 --revision EXACT_CURRENT_REVISION \
  --decision repeat-read --reason '已确认是只读资料查询，允许新的读取尝试'
```

`repeat-read`保留原未知尝试，新的读取可能观察到更新后的文件；
`discard-request`放弃该次未知模型输出，不证明它未执行或已经取消；
`abandon`明确终止任务。处置本身不发请求，之后再显式resume。
未经显式迁移的客户端绑定或执行源码变化会拒绝继续，不能编辑数据库绕过检查。

代码升级或同客户端API Key轮换，先生成私有计划并查看计划中的旧/新
源码、Key指纹、任务revision和剩余预算，再用返回的plan_sha256应用：

```bash
python3 -B tools/project_tasks/runner.py plan-migration \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha-new.json \
  --key release-question-001 --plan .q4t-work/alpha-handoff.json \
  --reason '升级至已验任务实现，保持同一客户端和任务合同'
python3 -B tools/project_tasks/runner.py apply-migration \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha-new.json \
  --key release-question-001 --plan .q4t-work/alpha-handoff.json \
  --plan-sha256 EXACT_PLAN_SHA256
```

计划父目录须私有0700，计划0600；生成后任务变化则需新计划。迁移
只追加事件，重复apply返回已应用，不执行HTTP/工具；命令退出0表示
计划/迁移操作成功，不表示业务任务完成。之后用新profile显式resume。
已知结果复用、未知状态及原预算保持；旧Key不能继续操作新绑定任务。

当前登记已验SRV-11到SRV-11g的精确源码来源；同client Key轮换仍只
能使用原CA，不允许同时换client、TLS origin/IP、模型/协议/模板、
问题或预算。
未知源码须先审查适配。实际Key撤销仍需先准备新网关私有包、停旧
网关并确认清理、再运行新包；迁移命令不管理服务，不证明新Key的
远端client映射。原因中不要填写凭据。详见
[迁移合同](../../docs/SERVE_TASK_MIGRATION_2026-11-04.md)。

已审核的协议升级使用独立`plan-protocol`/`apply-protocol`，参数与上面
迁移命令相同，新profile仅改变protocol_sha256。先停妥旧任务进程，
核对计划旧/新协议、版本对的审核依据、revision、来源及剩余预算，
再显式apply，之后才resume。已登记63b29f13→fc63db28，以及
fc63db28→ddfba8c0两个有向步骤；后一项23 CPU、2入口检查及两版本
真实恢复已[限定接受](../../docs/SERVE_TASK_OPERATIONS_RESULT_2026-11-04.md)。
新增ddfba8c0→15d7ccb1的独立监听生命周期审核与11j精确来源登记，
当前验收进展见`docs/STATUS.md`。不自动跳过步骤；各次明确计划/应用
均计入共享8次迁移限额。启动TCP可早于HTTP就绪，恢复仍需重新预检。
没有直接登记63b29f13→ddfba8c0，需两次明确计划/应用并分别计入原8次
迁移上限。登记不触发自动升级，
完整摘要和依据见`compatible_protocols.json`；不推导未登记方向兼容。
同client/origin/IP、CA、Key、模型/模板必须保持，普通Key或CA迁移
不能代替协议计划。活跃、终态、归档和已清正文的永久键均覆盖，
未知执行仍不重放。准备和应用均不联网，不能证明目标服务已部署；
恢复会重新发现能力并逐轮预检。参见
[协议迁移合同与当前验收边界](../../docs/SERVE_TASK_PROTOCOL_2026-11-04.md)。
[两版本真实恢复已限定接受](../../docs/SERVE_TASK_PROTOCOL_RESULT_2026-11-04.md)：
旧应用已保存的工具结果不重放，新版只续最终答案。归档协议迁移的
接受路径以CPU事务/进程中断证据为准；真实模型轮验证归档去重和反向
拒绝。已迁移账本不能用旧读者继续执行，不通过删除事件或回滚预算降级。

CA交接使用独立的显式计划，见[信任迁移合同](../../docs/SERVE_TASK_TRUST_2026-11-04.md)。
[双CA真实恢复与归档保键短验已限定接受](../../docs/SERVE_TASK_TRUST_RESULT_2026-11-04.md)。
新profile仅更换ca_file对应材料，client、origin/IP、Key、模型、
协议/模板保持；先停妥旧任务进程，核对计划旧/新CA SHA、revision、
来源及预算，再应用摘要。不能用普通apply-migration应用信任计划。

```bash
python3 -B tools/project_tasks/runner.py plan-trust \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha-new-ca.json \
  --key release-question-001 --plan .q4t-work/alpha-trust-plan.json \
  --reason '同一服务更换经运维确认的CA，保留已保存业务结果'
python3 -B tools/project_tasks/runner.py apply-trust \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha-new-ca.json \
  --key release-question-001 --plan .q4t-work/alpha-trust-plan.json \
  --plan-sha256 EXACT_PLAN_SHA256
```

信任计划不联网、不管理网关、不撤销旧CA。应用后再显式resume，
完整TLS/主机名校验、能力发现与预算预检仍会执行；未知请求/工具
保持UNKNOWN，不能借迁移重放。CA来源需由运维独立确认，计划仅
记录显式配置选择。更换Key和CA必须分开操作，不能混成一次迁移。
已完成任务保持答案，归档/已清正文的永久键只追加信任绑定事件，
不改正文或原合同摘要。重复apply/submit保持零执行，旧绑定拒绝。

profile读取至多65536字节、1至8张PEM证书并检查包含CA；拒绝
非证书附带数据。摘要与TLS上下文使用同一不可变材料快照，运行中
原文件替换不改变该进程的信任集。新进程读取新材料后仍需匹配任务
绑定。上下文不继承系统默认CA或SSLKEYLOGFILE，不关闭验证。只接受
由profile返回的已验证快照来构造内部Client。

要回收终态任务占用的活跃名额，先停妥该store的旧版任务客户端，
再显式启用留存；之后只用支持留存的版本访问，不能回退旧代码继续
使用扩展store。原store_id不变，已有任务不自动归档。默认策略是
4096永久键、1GiB归档计额和正文最少保留一天；参数只在启用时选择。
max_keys覆盖活跃及已归档任务。目录簿/回滚预留65MiB，剩余空间
还计入键与正文；最小归档配置66MiB，满额拒绝而不自动清理。

```bash
python3 -B tools/project_tasks/runner.py enable-retention \
  --root .q4t-work/project-tasks --max-keys 4096 \
  --archive-bytes 1073741824 --min-retain-seconds 86400
python3 -B tools/project_tasks/runner.py retention-status \
  --root .q4t-work/project-tasks
```

仅COMPLETED/FAILED/ABANDONED可归档。计划放在store外的私有0700
目录；检查原revision、客户端、文件身份与策略后，按返回SHA应用。
完整原账本/响应整体移动，释放活跃名额，永久键继续计入max_keys。

```bash
python3 -B tools/project_tasks/runner.py plan-archive \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha.json \
  --key release-question-001 --plan .q4t-work/private/task-archive.json \
  --reason '任务已终结，保留原件并回收活跃名额'
python3 -B tools/project_tasks/runner.py apply-archive \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha.json \
  --key release-question-001 --plan .q4t-work/private/task-archive.json \
  --plan-sha256 EXACT_PLAN_SHA256
python3 -B tools/project_tasks/runner.py inspect-archive \
  --root .q4t-work/project-tasks --profile .q4t-work/alpha.json \
  --key release-question-001
```

正文保留期到期后，用相同流程的plan-prune/apply-prune显式清理。
计划文件名另取新名，原因说明清理依据；只删除精确封存的正文文件，
不删除键。重复submit/resume返回ARCHIVED且退出2，不返回答案或
再次执行；只有inspect-archive在正文仍在时返回原答案，清理后只
返回payload_state=purged。同键修改问题/预算会拒绝。归档任务的
同client Key轮换继续使用plan-migration/apply-migration，CA交接用
plan-trust/apply-trust，已审核协议升级用plan-protocol/apply-protocol。
三类共用原8次归档绑定变更上限；只更新绑定链，不改正文，也不发送
HTTP或撤销远端Key/CA。

如操作中断，retention-status返回RETENTION_PENDING和operation_sha256。
核对操作日志后按该摘要恢复；未恢复前普通提交/查找拒绝。若事务在
提交前中断，状态仍READY且原任务未移动，可重新应用原计划。

```bash
python3 -B tools/project_tasks/runner.py finish-retention \
  --root .q4t-work/project-tasks --operation-sha256 EXACT_OPERATION_SHA256
```

finish仅保存最近一次完成回执；同计划apply按该键历史核对已执行。
这些管理命令退出0只表示管理操作成功，不表示新业务完成。正文
清理前如需备份，可用下方完整快照入口；当前不提供策略扩容命令，
不能手改store/目录簿或删键绕过准入。

完整备份会短暂锁住整个store及其任务；请先停妥旧版客户端，并选择
没有活跃任务的维护窗口。忙锁、未完成留存操作、未知文件或容量
不足时拒绝，不强行停止进程。先创建0700私有父目录，显式保存到
新快照目录；备份包本身不能当作可执行任务库。

```bash
python3 -B tools/project_tasks/backup.py backup \
  --root .q4t-work/project-tasks \
  --snapshot .q4t-work/private/task-backup-001 --max-bytes 2147483648
python3 -B tools/project_tasks/backup.py verify \
  --snapshot .q4t-work/private/task-backup-001 \
  --snapshot-sha256 EXACT_SNAPSHOT_SHA256
```

verify不依赖模型/HTTP，也不会恢复任务。输出SHA是manifest内容摘要，
逐文件原件还会全部核对；备份包含业务正文，继续按私有资料保存。
未生成seal的草稿不可使用；源保持完全一致时可再次backup继续。
源已有新写则需新快照目录，旧草稿保留，不自动删除或收编异常文件。

需要更换任务库路径时，先停写，用刚完成的快照生成交接计划。源
路径必须仍可逐项核对且完全未变；源不可达或已变化会拒绝，不能
将旧快照强制激活。目标必须不存在，禁止覆盖旧库或放在源/快照内部。

```bash
python3 -B tools/project_tasks/backup.py plan-restore \
  --root .q4t-work/project-tasks \
  --snapshot .q4t-work/private/task-backup-001 \
  --snapshot-sha256 EXACT_SNAPSHOT_SHA256 \
  --destination .q4t-work/private/project-tasks-restored \
  --plan .q4t-work/private/store-restore.json \
  --reason '维护窗口内迁移任务库，保持原业务与幂等记录'
python3 -B tools/project_tasks/backup.py apply-restore \
  --plan .q4t-work/private/store-restore.json --plan-sha256 EXACT_PLAN_SHA256
```

命令先准备不可执行的目标、更新物理身份，再持久封源并原子发布
目标。之后所有任务命令显式换用新root；原profile/key/预算不改，
源码升级仍使用原迁移合同。已知结果复用，UNKNOWN仍需原有显式
处置，恢复不表示未知调用未发生。归档键增加placement身份记录，
原事件/正文/保留期保持；已清理的正文不会从另一个旧备份补回。

若中断，重新apply相同计划和摘要；目标已发布时返回已应用，不
覆盖新任务结果。封源后只能显式继续该交接，不能删handoff.json
或改store.json来启用旧路径。未完成的初始化元数据可能需要保留
草稿并另做新计划；源已封时须保留原工作目录并继续原计划。
源/快照/交接工作记录均不自动删除。最多8次交接，新增归档身份
记录仍计入原存储额度；本机交接不等同于源盘丢失后的灾备恢复。

store、SQLite账本和wire都是私有资料；同UID是可信边界。没有自动
清理、自动任务迁移、队列服务或后台恢复，不提供多机协调或通用exactly-once。
运行本入口不启动或停止模型服务；开发HTTP验证仍需在共享GPU租约内。

CA和已审核协议迁移状态以[STATUS](../../docs/STATUS.md)为准；模型/
模板/目标地址或未登记协议迁移、正式保留/容量变更及源盘丢失灾备
仍需明确策略。
旧版本账本可离线核验，但不能自动迁移到新执行源码。已保存的本机验收证据
可以离线核对，不启动模型或HTTP：

```bash
python3 -B tools/project_tasks/report.py verify
python3 -B tools/project_tasks/budget_report.py verify
python3 -B tools/project_tasks/migration_report.py verify
python3 -B tools/project_tasks/archive_report.py verify
python3 -B tools/project_tasks/backup_report.py verify
python3 -B tools/project_tasks/protocol_report.py verify
```
