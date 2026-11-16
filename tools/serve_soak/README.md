# 原生服务持续运行验收

当前入口为`native_stage.py`，直接使用精确原生服务包和outer/Guardian，
不创建systemd unit，不使用nginx。14项CPU已通过；实际short接受状态
以[STATUS](../../docs/STATUS.md)为准。24小时由用户显式执行，agent只
运行short。完整合同见[原生交接计划](../../docs/SERVE_NATIVE_SOAK_2026-11-16.md)。

在干净且已提交的工作区，为当前包准备两个不同目录；prepare/check
不启动模型，不改变实际默认及生产服务状态：

```bash
python3 -B tools/serve_soak/native_stage.py prepare --profile short \
  --package build/service --output .q4t-work/operator-native-soak/short-01
python3 -B tools/serve_soak/native_stage.py prepare --profile day \
  --package build/service --output .q4t-work/operator-native-soak/day-01
python3 -B tools/serve_soak/native_stage.py check \
  --plan .q4t-work/operator-native-soak/day-01/plan.json
```

选择新的目录保留每次计划；来源/包/默认变化会拒绝旧plan，不删掉旧
execution重试。已接受short且你准备好长时间占用GPU后，显式执行：

```bash
python3 -B tools/serve_soak/native_stage.py run \
  --plan .q4t-work/operator-native-soak/day-01/plan.json
python3 -B tools/serve_soak/native_report.py \
  --output .q4t-work/operator-native-soak/day-01
```

run自己启动严格GPU guard并把原锁交给原生服务，不额外套另一把锁。
它前台运行；不自动重放请求或工具。day为24小时，每60秒一组JSON、
SSE、schema、真实文档工具和SSE续答，共1440组/7200业务生成加1自检；
保持当前alpha配额。第1小时60组/300请求只写独立检查点，不宣称day
通过。short为60秒/3组/15业务生成加1自检，最多600秒/1模型/0探测。

progress.json只作进度，不能证明进程仍活着。计划目录handoff.json
包含绑定原包/原私有状态的确切status和stop命令；终端丢失时执行其中
的stop，再核对status。只操作这个验证服务，不能按旧PID批量kill。
驱动/证据/外层监控失败保留原始记录，ENV_ABORT不等于核心模型故障；
不要清空锁或recovery标记。正常结束需原生STOPPED、整树/端口/锁清理，
并通过原始wire和工具来源、连续监控日志、计数/资源及密封文件复核。

report只读，不启动模型。内存增长只描述原模型进程，RSS与RssAnon
相互重叠；不声称统一DRAM硬上限。短验也不声称24小时或无限期无泄漏。

## 历史systemd入口和证据

以下仅为2026-11-03原包的历史命令/证据，不能用它们验收当前原生包。

用户专门负责长时间验证；agent只做开发和short验证。默认profile为
short，所有命令都需要显式执行，没有timer、开机项或恢复脚本。
范围与限额见[冻结合同](../../docs/SERVE_SOAK_2026-11-03.md)。

2026-11-03已完成CPU11及short 8周期/40成功、8次真实工具回填，
详见[短验结果](../../docs/SERVE_SOAK_RESULT_2026-11-03.md)。下面的CPU和
两个prepare已执行，现有计划可直接check；不要重复准备或覆盖原件。
day尚未启动；其第1小时与24小时稳定性结论须等你的实际运行结果。

准备工作在工作区干净且来源已提交时运行，不加载模型：

```bash
python3 -B tools/serve_soak/stage.py cpu
python3 -B tools/serve_soak/stage.py prepare --profile short
python3 -B tools/serve_soak/stage.py prepare --profile day
```

这些目录只能创建一次；已有CPU/plan/execution时拒绝覆盖，不删除
旧目录重试。`check`只核对来源、包和默认身份。真实执行使用包内
生产GPU guard贯穿整轮；不要额外套第二把锁，否则自身会竞争。

用户长稳开始前，在项目根目录运行：

```bash
python3 -B tools/serve_soak/stage.py check --profile day
python3 -B tools/serve_soak/stage.py run --profile day
```

`run`前台保持，GPU不足/占用/恢复标记不会被自动清除。Ctrl-C会保留
失败/中断证据并停止本轮临时unit，不重放请求。若操作终端丢失，可
在新终端显式停止本轮：

```bash
/usr/bin/python3 -I -B tools/serve_service/service.py stop \
  --run .q4t-work/serve-soak-20261103/day/execution/managed-run
/usr/bin/python3 -I -B tools/serve_service/service.py inspect \
  --run .q4t-work/serve-soak-20261103/day/execution/managed-run
```

查看`.q4t-work/serve-soak-20261103/day/execution/progress.json`获取当前
周期/请求数与PID；文件本身不能证明进程仍活着，配合上述inspect和
systemd实际状态。第一小时到达240周期/1200成功请求并满3600秒后写
`first-hour.json`，它不会宣称day已通过。最终`result.json`、`soak.json`、
`capture/capture.json`和`guard-archive.json`共同判定；不只看进度或PASS
字样。完整wire、quiescent样本、连续资源与轮转日志副本均留在execution。

正常结束后运行只读复核，重新解析完整响应、工具引用及清理证据，
不会启动模型或发送HTTP：

```bash
python3 -B tools/serve_soak/report.py check --profile day
```

若driver被强杀，临时unit最多运行到计划的RuntimeMaxSec，不能假定
已自动清理；不要按旧PID批量kill。用manifest中的unit名核对身份后
停止，保留原GPU锁inode与原始失败记录。正常完成会删除临时unit并
释放GPU/锁，SOAR恢复服务保持masked。此入口不修改默认ELF/sidecar。
