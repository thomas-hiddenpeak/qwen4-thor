# 受管运行目录与私有日志留存

为后端和 TLS 网关提供显式启动、跨运行目录预算、封存、私有读取和
计划清理。它不修改默认 ELF、固定服务包或历史验收目录；没有自动
启动、定时清理、服务恢复或请求重放。现有 day 入口仍按原计划工作，
不会因为新增工具而自动迁移日志或开始运行。

[合同](../../docs/SERVE_RETENTION_2026-11-04.md) ·
[本机短验结果](../../docs/SERVE_RETENTION_RESULT_2026-11-04.md)

## 显式接入

以下是操作方法，命令不会自动执行。先创建一个全新 store；已有目录
会拒绝，不把已有运行目录或验收原件导入本工具的删除范围。

```bash
python3 -B tools/serve_retention/runner.py init \
  --root .q4t-work/production-generations \
  --max-bytes 268435456 --max-generations 16 --max-age-days 14
```

启动后端时使用已经接受的包路径及其 manifest SHA256。`run` 前台保持，
后端包内生产 controller 自带 GPU 独占 guard，不要再套一层 guard。
容量或 GPU 竞争不会触发清理其他任务、重试或自动恢复。

```bash
python3 -B tools/serve_retention/runner.py run \
  --root .q4t-work/production-generations --kind backend \
  --package /absolute/backend-package --manifest-sha256 BACKEND_SHA256
```

在另一个终端列出代次，得到 `g-...` 名字。`ACTIVE` 仅表示代次锁被
持有，不表示模型已经 READY；用固定包的 service.py status 检查其
`<store>/generations/<generation>/run` 后，再启动匹配的网关包。

```bash
python3 -B tools/serve_retention/runner.py list \
  --root .q4t-work/production-generations
python3 -B tools/serve_retention/runner.py run \
  --root .q4t-work/production-generations --kind gateway \
  --package /absolute/gateway-package --manifest-sha256 GATEWAY_SHA256 \
  --backend-run /absolute/store/generations/g-BACKEND/run
```

后端启动前预占 96MiB，网关预占 32MiB；活跃和 UNSEALED 代次均按
预占额计账。SEALED 代次按已经核验的文件字节计账。代次数、文件
字节或观察不符合约定时拒绝继续，不为了继续服务自动抹除记录。
这不是文件系统 quota：文件系统块/元数据、包、模型、其他目录和
外部缓存另计，运行中四次/秒的观察也不是原子硬限额。

## 停止与失联

正常顺序为先停止网关、等待退出，再停止后端。每个终端 Ctrl-C，
或按实际 generation 名显式停止；成功发送信号不等于退出确认。

```bash
python3 -B tools/serve_retention/runner.py stop \
  --root .q4t-work/production-generations --generation g-EXACT_NAME
```

正常退出后生成 `seal.json`，绑定完整文件内容和进程清理证明；
`wrapper-terminal.json` 在封存之前记录执行结果，封存事实以独立的
`seal.json` 和 `list` 的完整核验为准。失败但已经确认清理的代次可以
封存，原 FAILED 状态保留。

wrapper 死亡、缺少终态或进程状态不明会留下 UNSEALED，占用完整
预留额，不因过期而删除。如果固定服务入口已经自行清理并留下可
核对终态，可显式执行只读进程核验，再新增封存证明：

```bash
python3 -B tools/serve_retention/runner.py reconcile \
  --root .q4t-work/production-generations --generation g-EXACT_NAME
```

reconcile 不发信号、不修改旧状态；证据不足时继续拒绝。不要删除
锁文件、伪造终态或用历史 PID 批量 kill。它不是通用失联恢复器。

## 私有读取与计划清理

原日志不承诺脱敏。读取仅适用于已封存代次，输出到 store 外的新
0600 文件，默认 64KiB、最多 256KiB；终端只显示路径、字节和 SHA。

```bash
python3 -B tools/serve_retention/runner.py tail \
  --root .q4t-work/production-generations --generation g-EXACT_NAME \
  --file run/backend.log --tail-bytes 65536 \
  --output .q4t-work/backend-tail-private.txt
```

TTL 是候选策略，不会触发后台删除。生成并检查具体计划后，显式
执行；一次最多处理两个已封存代次。库存、根目录或文件身份变化
会拒绝旧计划，活跃和未知记录始终不在清理范围。

```bash
python3 -B tools/serve_retention/runner.py plan \
  --root .q4t-work/production-generations --output .q4t-work/prune-plan.json
python3 -B tools/serve_retention/runner.py prune \
  --root .q4t-work/production-generations --plan .q4t-work/prune-plan.json
```

删除前会持久化 `gc.json`，之后原子改名并删除。若操作中断，会阻止
新启动；下面的命令仅继续该意图下已授权、未被修改的剩余记录。
出现新增或篡改文件时保留现场，不扩大删除范围。

```bash
python3 -B tools/serve_retention/runner.py resume-prune \
  --root .q4t-work/production-generations
```

正式私有日志保留周期和访问人员需要按部署配置；本机短验不等于
实际内网访问、整机磁盘硬保护或小时/天级稳定性证明。短验证据可离线
核对，不启动服务或 HTTP：

```bash
python3 -B tools/serve_retention/report.py verify
```
