# 当前版本持续运行验收

用户专门负责长时间验证；agent只做开发和short验证。默认profile为
short，所有命令都需要显式执行，没有timer、开机项或恢复脚本。
范围与限额见[冻结合同](../../docs/SERVE_SOAK_2026-11-03.md)。

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

若driver被强杀，临时unit最多运行到计划的RuntimeMaxSec，不能假定
已自动清理；不要按旧PID批量kill。用manifest中的unit名核对身份后
停止，保留原GPU锁inode与原始失败记录。正常完成会删除临时unit并
释放GPU/锁，SOAR恢复服务保持masked。此入口不修改默认ELF/sidecar。
