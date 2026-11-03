# 持久项目查询任务

这是受控本地应用入口，使用 TLS 网关和一个客户端的独立 API Key。
工具仅查询本项目允许范围内的真实文档、发布和验收记录。
[合同](../../docs/SERVE_PROJECT_TASKS_2026-11-04.md)。

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
  --max-requests 4 --max-tools 8
```

任务完成会返回答案和outcome；全部读取失败是UNVERIFIED，不是已核实
的业务答案。退出0表示任务流程完成，退出2表示需要继续或处置，退出1
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
客户端绑定或执行源码变化会拒绝继续，不能编辑数据库绕过检查。

store、SQLite账本和wire都是私有资料；同UID是可信边界。没有自动
清理、任务迁移、队列服务或后台恢复，不提供多机协调或通用exactly-once。
运行本入口不启动或停止模型服务；开发HTTP验证仍需在共享GPU租约内。
