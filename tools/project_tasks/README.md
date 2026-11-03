# 持久项目查询任务

这是受控本地应用入口，使用 TLS 网关和一个客户端的独立 API Key。
工具仅查询本项目允许范围内的真实文档、发布和验收记录。
[合同](../../docs/SERVE_PROJECT_TASKS_2026-11-04.md) ·
[本机真实恢复验收](../../docs/SERVE_PROJECT_TASKS_RESULT_2026-11-04.md)。
[双客户端预算与恢复预检验收](../../docs/SERVE_PROJECT_TASK_BUDGET_RESULT_2026-11-04.md)。
[源码升级与同客户端Key轮换验收](../../docs/SERVE_TASK_MIGRATION_RESULT_2026-11-04.md)。

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

当前只登记已验SRV-11/SRV-11b到本版的源码升级及同client Key轮换；
不允许换client、TLS origin/IP/CA、模型/协议/模板、问题或预算。
未知源码须先审查适配。实际Key撤销仍需先准备新网关私有包、停旧
网关并确认清理、再运行新包；迁移命令不管理服务，不证明新Key的
远端client映射。原因中不要填写凭据。详见
[迁移合同](../../docs/SERVE_TASK_MIGRATION_2026-11-04.md)。

store、SQLite账本和wire都是私有资料；同UID是可信边界。没有自动
清理、自动任务迁移、队列服务或后台恢复，不提供多机协调或通用exactly-once。
运行本入口不启动或停止模型服务；开发HTTP验证仍需在共享GPU租约内。

CA/模型/协议或未登记版本迁移、终态归档后幂等键保留仍需明确策略。
旧版本账本可离线核验，但不能自动迁移到新执行源码。已保存的本机验收证据
可以离线核对，不启动模型或HTTP：

```bash
python3 -B tools/project_tasks/report.py verify
python3 -B tools/project_tasks/budget_report.py verify
python3 -B tools/project_tasks/migration_report.py verify
```
