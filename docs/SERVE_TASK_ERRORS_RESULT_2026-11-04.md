# SRV-11f：管理入口错误合同短验限定接受

2026-11-04。生产实现冻结 **225e82bd**，运行时15项源文件。按
[冻结合同](SERVE_TASK_ERRORS_2026-11-04.md)完成业务可靠性修复；
[原驱动失败后的离线续验](SERVE_TASK_ERRORS_FINISH_2026-11-04.md)补齐剩余
管理拒绝，不额外加载模型，不改写原FAILED记录。当前结论为
`ACCEPT_PROJECT_TASK_ERRORS_SHORT`，不表示整个serve工业化目标完成。

## 交付行为

runner/backup的CLI错误使用`q4t-project-error-v1`，保留规范化`error`
类别，新增固定code/category/message/next_action/operation/http_status。
SQLite、OSError、远端错误及意外KeyError/TypeError等统一安全出口；
原异常/堆栈/路径/argv/HTTP错误正文不回显。常见容量、锁忙、幂等、
绑定/运行时迁移、计划/快照过期、保留中断、封源、认证和传输可区分。
未专门分类的合同/意外错误仍保守返回contract_violation/internal_error。

逐命令拒绝缺少、无关、重复、缩写参数。参数错误现在退出1，原来
argparse是文本/退出2；键盘中断130，帮助0。有效任务状态的0/2语义
保持。`automatic_retry=false`，处置提示不执行操作，不推断请求没有
提交。REQUEST_UNKNOWN/TOOL_UNKNOWN仍须按revision显式resolve。
同机备份、归档和迁移数据格式不变；11e的14项来源已精确登记，已有
任务升级须显式迁移，不能直接绕过绑定。默认C++程序与发布元数据未改。

## 检查与保留的失败

| 组 | 原始结果 | 有效证据 |
|---|---|---|
| 应用CPU首轮 | 106项：88通过，新增18项均在setup失败 | 原CA夹具少一层gateway目录，尚未进入断言；完整源码/日志保持 |
| 定向夹具修正 | 仅重跑18项，全部通过 | 共106有效应用项，真实flock、SQLite忙/满/只读/损坏、隐私canary、过期/封源、未知不重试 |
| 原驱动CPU | 2项通过 | 环境竞争拒绝及异常清理顺序 |
| 旧数据/故障 | 23次文件操作SIGKILL通过；五代411/263/468/402/368原件离线重建 | 使用身份不变的既有合同，不重复原模型推理 |
| 真实短验 | 4成功/2工具已通过，驱动在导出认证任务时失败 | 配置别名denied被误当作client_id；实际是beta。原FAILED/11控制/3拒绝保持 |
| 驱动定向回归 | 2项通过 | 按绑定导出及网络/工具禁执行标记 |
| 离线续验 | 5条CLI、0.661044秒通过 | 补3拒绝、1认证任务重复提交和1只读计划；0HTTP/0工具/0GPU，任务库与原证据不变 |

106是有效应用用例数，不把失败重跑累计成新覆盖；2原驱动+2定向驱动
分别记录。未把原短验FAILED改为PASS，也未将离线拒绝计为真实HTTP请求。

## 真实业务与资源

仅1模型/1TLS网关。alpha512和beta64各2生成、1只读资料工具；两者
均读`docs/MODEL.md:L1-L24`，返回实际`Qwen4ExpForConditionalGeneration`
及正确引用。各轮输出55/58 token，prompt647/890；共4预检、4请求/
响应、2工具。正常2能力查询，另1认证失败能力查询返回真实401，
网关未转发它。认证任务仅保存run=1、READY、0请求/工具，无wire。

6种管理错误：真实阶段的key_conflict、invalid_arguments、remote_auth；
离线续验的binding_changed、task_capacity、plan_changed。累计3重复
submit、2终态resume无额外执行。活跃任务库保留3任务（2完成+1认证
失败后READY），无归档键、无已执行数据删除；只生成外部归档计划。

原驱动在终场`/metrics`采集前失败；终场metrics缺失，不补造零值。
后端实际日志有4个成功request end与slot released、4次grammar worker
reaped、shutdown 0 in-flight；响应ID逐一对应持久账本。网关11条真实
记录重建2能力成功、4预检+4生成成功、1认证401。最后监督轮询仍可能
在第4请求中，不作为终场空闲证明。请求end日志用时合计15.839094秒，
只记有界业务观察，不作性能接受或Prometheus统计解释。

原阶段总60.833820秒，GPU同一租约60.031491秒/57观察无竞争；原11
控制加离线5命令共16。7自有进程身份均退出、2代次SEALED，GPU计算
上下文空、原锁dev66305/inode3294067释放；SOAR恢复单元仍
masked/inactive/dead/MainPID0、容器空。离线续验再次核对自有身份无复起。
默认ELF/sidecar、固定服务包、原API Key、旧证据与三份day保持；day未跑。
轮询不能排除短于采样间隔的干扰；未验证长期稳定性与实际LAN接入。

## 可重建交付

[结果](evidence/serve-task-errors-20261104/result.json)与
[清单](evidence/serve-task-errors-20261104/manifest.json)封存395原件、
2 ELF本机路径身份；records.tar.gz 814488字节，SHA256：
`c3e96394ef0d00e5278018ece322ed46a61d0f8ed8774a7b59be24d0d4d82771`。
包含原CPU失败/修正、原真实失败与续验、完整任务SQLite/check/wire、
服务日志/guard/清理、对应源码。私有真实/错误测试凭据与私钥未入档。

```bash
python3 -B tools/project_tasks/error_report.py verify
```

395原件离线重建通过，不加载模型、不发HTTP。原工作目录：
`.q4t-work/serve-task-errors-20261104/`；原驱动按冻结记录保留，继续完成
的入口是`error_finish.py`，它拒绝新模型/HTTP/工具执行，不重复业务。
225e82bd前的代码不能自动接管新runtime绑定的任务；回退时保留任务
及匹配源码，不承诺任意跨版本降级。已登记来源可显式升级，不能手改
runtime_sources或清理永久键绕过。提交与证据提供可核验版本边界，
默认程序无须替换。

实际LAN参数/部署、正式容量/备份介质政策、源盘丢失灾备、物理RAM
资源边界和用户长期验证仍独立待办；本批只接受上述CLI错误及短业务范围。
