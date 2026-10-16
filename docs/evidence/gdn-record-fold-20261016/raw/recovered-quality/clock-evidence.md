# 中断质量轮的时钟证据（2026-10-16）

## 结论与范围

本次仅做只读系统时钟、客户端源码和既有证据检查，没有运行 GPU、HTTP、
测试或构建，没有修改原 DB、journal、source 或原始中断记录。
本文保存审阅过程中已经取得的采样，没有为本文补跑请求或构造请求时间。

**可复用质量结果为 0 条，缺失质量证据为 11 条。** 原始数据库以
`mode=ro&immutable=1` 打开时 `result` 为 0 行；另一个 agent 对复制的
DB 和同名 journal 恢复后，`db-recovery-inspection.json` 同样记录 0 行。
因此无法逐条证明任何请求的响应、质量或完成时刻，更无法逐条证明它们
完成早于竞争进程。进度日志的 `10/11` 只保留为处理进度，不计 10 条 PASS。

## 证据来源

相对本文件所在目录的文件：

- `../http-clean/environment-abort.json`：竞争 PID、启动 ticks 和中断记录。
- `../http-clean/quality-on-environment-monitor.json`：73 次轮询、2 秒间隔、
  自有 runner 已退出、只对自有 runner 发送 SIGINT。
- `../http-clean/quality-on/events.jsonl`：runner 的 wall/perf_counter 配对。
- `db-recovery-inspection.json`：复制数据库恢复后 `row_count=0`。
- `raw-db-identity.json`：原始 DB/journal 的摘要及复制前后未变化检查。

原始客户端 DB 为 `../http-clean/quality-on/quality/20261016_115608/`
`qwen3.8-flash-next/parallel_1_number_11/benchmark_data.db`，8192 字节；
同名 `-journal` 为 8720 字节。只读查询能读取 schema，但无请求行。

本地实现依据：

- `tools/evalscope/run_acceptance.py:31`：`clock_sample()` 在两次
  `perf_counter()` 之间采 `time.time()`，以中点和半区间记录配对误差。
- `tools/evalscope/.venv/lib/python3.12/site-packages/evalscope/perf/`
  `plugin/api/default_api.py:155`、`:183`、`:220`：请求及流式消息时间
  使用 `perf_counter()`；`completed_time` 是最后解析 JSON 消息的时刻，
  不是独立 wall-clock 样本，也不能额外解释成 TCP 关闭时间。
- 同一安装的 `perf/arguments.py:213`：`db_commit_interval` 默认 1000；
  `perf/core/metrics_consumer.py:58`、`:119` 起和 `:159`：逐行 INSERT，
  达到提交阈值或正常结束循环后才 commit。10 条处理进度不保证它们已提交。

## 已取得的系统时钟采样

运行内核为 `6.8.12-1021-tegra`。本次只读采样返回：

```text
os.sysconf('SC_CLK_TCK') = 100
time.get_clock_info('perf_counter'):
  implementation = clock_gettime(CLOCK_MONOTONIC)
  monotonic = True
  adjustable = False
  resolution = 1e-09
/proc/sys/kernel/random/boot_id = 67aedfc7-025a-4a36-afe3-58ec3fc670d8
/proc/self/timens_offsets:
  monotonic 0 0
  boottime  0 0
clocksource = arch_sys_counter
```

以下为已执行的四组括号采样原值。每组顺序为
`mono_before=perf_counter()`、`boot=clock_gettime(CLOCK_BOOTTIME)`、
`mono_after=perf_counter()`，最后读取 `unix=time.time()`。

| 组 | mono_before | boot | mono_after | unix | boot−mono 区间（秒） |
|---|---:|---:|---:|---:|---|
| 1 | 313371.819322901 | 313371.819324290 | 313371.819324540 | 1792123289.3024511 | [-0.000000250001904, 0.000001388951205] |
| 2 | 313371.819347725 | 313371.819348114 | 313371.819348346 | 1792123289.3024740 | [-0.000000231957529, 0.000000389001798] |
| 3 | 313371.819357873 | 313371.819358142 | 313371.819358299 | 1792123289.3024840 | [-0.000000157044269, 0.000000268977601] |
| 4 | 313371.819364901 | 313371.819365105 | 313371.819365244 | 1792123289.3024910 | [-0.000000138999894, 0.000000204017852] |

区间公式为 `[boot − mono_after, boot − mono_before]`。各区间都包含 0，
与本 boot 无可观测 suspend 偏移一致；不是预设两个时钟恒等。
若以后出现 suspend、换 boot 或时间命名空间变化，不能直接复用本映射。

Linux `CLOCK_BOOTTIME` 包含 suspend 时间，而 `CLOCK_MONOTONIC` 不包含。
上游 fork 保存 `start_boottime=ktime_get_boottime_ns()`，`/proc/PID/stat`
输出经过时间命名空间校正和 tick 转换的启动时间。
依据：[Linux 时钟说明](https://docs.kernel.org/core-api/timekeeping.html)、
[Linux v6.8 fork.c](https://github.com/torvalds/linux/blob/v6.8/kernel/fork.c#L2458)、
[Linux proc array.c](https://github.com/torvalds/linux/blob/master/fs/proc/array.c)。
这是上游语义依据；没有把它表述为已逐行审核本机 NVIDIA 内核源码。

## 竞争进程和中断边界

这次中断记录只包含一个竞争者：

```text
pid: 1031522
ppid: 1031514
start_time_ticks: 31316426
executable: /home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-decode-20261016/q4t-main
argv: serve --model-dir <既有只读模型目录> --port 8730
      --max-seq 1 --max-prefill 8192 --max-len 208896
      --max-tokens 256 --no-mtp
```

因此**已观测竞争者**的最早启动 BOOTTIME 区间为：

```text
start_lower = 31316426 / 100 = 313164.26 seconds
start_upper = (31316426 + 1) / 100 = 313164.27 seconds  # exclusive
```

10 ms 是启动 tick 的量化宽度。不得将上界作为最早可能启动时刻，也不得
用 2 秒轮询发现时刻代替启动时刻。进程启动是其 GPU 工作开始之前的保守
边界；它不能给出实际 GPU 活动起点。

`events.jsonl` 的配对值：

| 事件 | monotonic_seconds | unix_seconds | 配对半区间误差（秒） |
|---|---:|---:|---:|
| client_before | 313048.63472976896 | 1792122966.1178553 | 0.000000309984898 |
| client_after | 313164.597197744 | 1792123082.0803237 | 0.000000601983629 |

由此 `unix − monotonic` 分别为 `1791809917.4831254` 和
`1791809917.483126`，两端偏移差约 `0.477 µs`。用 client_before 的配对
偏移展示启动区间，可得 UTC **估计**：

```text
竞争启动：2026-10-16T03:58:01.743125Z 至 03:58:01.753125Z（右端不含）
client_after：2026-10-16T03:58:02.080324Z
server_after_shutdown：2026-10-16T03:58:03.453667Z
最终 ENV_ABORT 记录：2026-10-16T03:58:03.495793Z
```

UTC 是用于展示的映射估计，不是竞争进程的独立 wall-clock 样本。
最终 ENV_ABORT 时间在 runner 退出后重写，不能当作首次发现竞争或首次
发送 SIGINT 的精确时刻。`client_after` 是客户端组的中断边界，
`client_returncode=null`；它不是 11 条请求都完成的证据。

## 若请求行存在时的判据及本次缺口

同一 boot、同一时间命名空间下，设 `delta_upper` 为
`BOOTTIME−MONOTONIC` 的保守上界，所有已观测竞争者启动 ticks 为 `s_j`，
则最早竞争启动的 MONOTONIC 下界为：

```text
earliest_competitor_lower = min(s_j / SC_CLK_TCK) − delta_upper
```

只有取得真实客户端结果行并验证
`client_before <= start_time <= completed_time < earliest_competitor_lower`
及合法响应结束标记后，才可证明相应已接收响应早于这些已观测竞争者启动。
还需独立验证正文、prompt hash、success、finish、token 数和 frozen 身份，
不能仅凭时间认定质量通过。2 秒 /proc 轮询是尽力检查，不能证明不存在
未被捕获的短暂或未匹配进程名的其他 GPU 工作。

**本次 result 为 0 行，不能代入任何请求的 start_time/completed_time，
没有逐条时间比较结论。** `11:57:11` 的 `10/11` 日志可以保留为处理进度，
但不补造响应、不分配假想请求时间、不转换成可复用质量结果。
