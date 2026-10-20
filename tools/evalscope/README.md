# HTTP E2E 验收

顺序与接受条件见 [EVALUATION.md](../../docs/EVALUATION.md)。性能优化
必要构建后的第一项测试为真实evalscope HTTP E2E，不使用bench。
Bug修复可先做针对缺陷的数值/差分测试；语义分数不能替代数值合同。

## 质量与同输入性能重放

使用已配置的 `tools/evalscope/.venv`。`run_acceptance.py` 拥有一次 serve
生命周期，默认普通 decode、max_seq=1、max_prefill=8192、max_len=208896。
端口需空闲；它不会结束其他进程或发送连接探测请求。

```bash
python3 tools/evalscope/run_acceptance.py --mode quality \
  --model-dir "$Q4T_MODEL_DIR" \
  --output .q4t-work/e2e/my-change-quality \
  --reference tools/evalscope/fixtures/quality_reference.json

python3 tools/evalscope/run_acceptance.py --mode performance \
  --model-dir "$Q4T_MODEL_DIR" \
  --output .q4t-work/e2e/my-change-performance \
  --reference tools/evalscope/fixtures/performance_reference.json
```

先把 Q4T_MODEL_DIR 设置为本机只读模型目录。输出目录必须尚不存在且位于
build/ 或 .q4t-work/。没有 --fixtures 时按固定算法生成语料；使用 --fixtures
可重放已有 quality-inputs/ 或含 context-N/requests.jsonl 的性能结果目录。
--binary 可选择保存的旧二进制；--port 可选择服务端口。

serve 默认开启 MTP 投机解码（k=3），`--no-mtp` 回退普通
greedy decode（性能基线侧）。run_acceptance.py 用 `--mtp` 显式
开启、`--mtp-default` 不带旗标（验证 serve 默认路径）、`--mtp-k`
指定 k；两者都不带时传 `--no-mtp`。两组使用不同
输出目录，`run-mode.json`、`server-command.json` 和结果中的
`decode_mode` 区分普通 decode 与 MTP。可分别用 `--binary` 指定旧版
和候选；`commit.txt`/`worktree.patch` 记录执行脚本所在源码树，实际
被测二进制以 `binary.sha256`、命令绝对路径和对应构建缓存为准，不能
把脚本所在提交自动当成外部旧二进制的构建提交。现有参考不会被重写。

```bash
python3 tools/evalscope/run_acceptance.py --mode quality --mtp \
  --binary build/runtime/q4t --model-dir "$Q4T_MODEL_DIR" \
  --output .q4t-work/e2e/my-change-mtp-quality \
  --reference tools/evalscope/fixtures/quality_reference.json
```

`--mtp`/`--mtp-default` 同样适用于 performance 和 limits 模式。
启动后记录并验证实际 capacity（`[q4t][capacity]`）：实际容量保持
1/8192/208896；预算缩小容量会直接拒绝。启动通过只证明加载状态。
数值验收合同为 [EVALUATION.md](../../docs/EVALUATION.md)「MTP vs
plain 近 tie 合同」（N=4 ULP、限定近 tie 为唯一分岔来源、causality
负控）；主线 sequential verifier 与 `[q4t][decode_path]` 终态日志
核验随 sequential 专属工件从归并树删除，其正确性参考价值保留在
main 提交历史。MoE trace 与 MTP 组合不在本入口接受范围。

质量模式包含 11 条原生模板检索题，要求明确答案精确匹配、实际输入计数
匹配并正常 stop。性能模式每档使用同一输入重复三次，要求实际五档长度、
256 输出、length 结束及重复文本一致。--reference 另外要求请求 prompt
摘要和生成文本/摘要与参考一致。失败返回非零并保留响应、数据库、服务日志；
运行中断保留退出信息，缺少结果不能视为通过。

每条响应还记录实际 HTTP/SSE `id`，要求有 choices 的消息均有合法且
一致的 ID；缺失、混合 ID 在保存 responses.json 后拒绝。不会以请求
序号推造 ID。evalscope 未保存的 usage-only SSE 不属于此项覆盖范围。
该合同和 physical_ram.py 的合成检查不需要模型，已注册公共 host CTest。
物理 RAM 工具只汇总提供的采样；证据不足返回 unknown，不以进程 RSS
替代整机使用量，也不把算法预算可行当作实际内存目标已满足。

性能结果同时保存 evalscope 数据库原值 `first_chunk_latency`（ttft）
与 `latency`（整请求耗时），并分别记录 `(输出数−1)/(latency−ttft)`
和 `输出数/latency`。前者是客户端 decode 估计，后者是全请求吞吐；
TTFT 包含接入、prefill 和 MTP 初始化，整请求耗时还含 HTTP 收尾。
MTP 可能成组输出 token，本工具不把 SSE chunk 间隔当成精确 token ITL，
也不据此提供 ITL 分位数或纯 GPU 阶段时间。非有限计时、非正 decode
区间在原响应记录保存后拒绝；原始数据库、逐请求响应及服务日志保留。

**脚本退出 0 不等于性能无回退已接受**：脚本验证 HTTP、长度、质量或输出
一致性并采集计时，性能仍需按总 decode token/总 decode 时间、每档重复
范围与对照版本分析；不得用任意固定容忍百分比替代现行接受条件。

## 有界资源探索与独立服务

`run_acceptance.py --mode performance --perf-lengths 45056 --perf-repeats 3`
只运行选定档位。plan/results/exit明确标为partial；不能替代五档或
offload六档验收。显式选档时每发启动一个evalscope client，服务保持
连续，客户端/tokenizer启动间隔与默认每档一次client的历史批次不同。
`events.jsonl`与`request-boundaries.json`记录客户端前后时钟和HTTP
单调时钟；HTTP的UTC是锚点映射估计。有效容量缺失或与请求值不一致
时，在发请求前拒绝；legacy预算日志不能单独证明有效prefill值。

`--systemd-unit q4t-UNIQUE.service`使服务出生于独立组，client和monitor
留在组外；`--host-cache-max-bytes BYTES`设置memcg限额，swap固定0。
本机需systemd与非交互sudo。Thor CUDA分配和外部已有缓存可能不计入
此组，**不能将它称为全部物理RAM上限**。服务正常/错误退出后用10秒
ExecStopPost窗口保留cg端点；辅助sleep计入unit尾部开销，准确模型PID
另行记录。退出或清理失败均非零，已有同名unit拒绝复用。

`run_budget_experiment.py`提供本轮冻结的C256/8192/262144、质量或45K×3
入口，集成逐文件缓存门禁、独立服务、监控和工具/输入身份快照：

```bash
python3 tools/evalscope/run_budget_experiment.py --mode performance \
  --binary build/q4t --model-dir "$Q4T_MODEL_DIR" \
  --hot-list .q4t-work/moe-residency-20260930/hot-lists/hot-final-12288.json \
  --fixtures .q4t-work/moe-residency-20260930/e2e-fixtures-v2 \
  --output .q4t-work/e2e/ram-host16g --clear-model-cache \
  --host-cache-max-bytes 17179869184
```

16GiB是本轮host/cache探索配置。冷态操作只向只读模型fd发送定向
eviction advice，必须cachestat确认safetensors/bin payload驻留0；
门禁失败保留现场并停止，请求间不清缓存/改额度。metadata可由组外
tokenizer重新缓存，不能冒称全部cache为独占charge。此工具不负责
选择新预算或自动扩大限额。协议与结论见
[RAM/缓存协议](../../docs/OFFLOAD_RAM_PROTOCOL_2026-10-02.md)。

monitor新增`--cgroup-path`，`resource-samples.jsonl`与CSV按sequence
对齐，保存真实PID读存储字节、cg io.stat/memory/限额/事件/PSI与
模型设备背景统计。分区与父盘不相加，服务IO含PLE/元数据等，
`nvme_mb`仍是逻辑读取。缺测/身份变化/计数重置保留null与原因。

## 固定参考的边界

quality_reference.json 来自 2026-09-20 完整数学修复后的真实 HTTP 结果，
对应 QSA 提交 c4717cc，二进制 SHA-256：
e98969c8d0d08572437745000d2a741a83b97d09600e60cf2ffd746643b3dcd3。
只证明这 11 道合成检索题，不是全面模型质量基准。

performance_reference.json 当前对应2026-09-28首版私有文本部署，二进制
`e659a108899b192c74eafcc484f3a38334d8999826cc4f8a21d07eeddadba7cb`。
来源、源码摘要与参数以performance_reference.metadata.json为准。
五档数据和必要修复的性能代价见
[收敛验收](../../docs/RELEASE_TEXT_V1_2026-09-28.md)，不在本入口复制数值。
它不代表严格无回退证明；此前8K配对差异记录继续保留。
上一正式参考保存在performance_reference_pre_text_v1_20260928.json
及同名metadata文件；更早的数值修复结论见
[E4M3报告](../../docs/E4M3_NUMERICAL_FIX_2026-09-27.md)。
quality_reference.json保持原有有界任务参考，不能当作整模型oracle。

下一阶段优先将候选构建到独立build/或.q4t-work/子目录，显式传
--binary，保留text-v1发布包不变。部署默认二进制的q4t.release.json
记录了真实验收包与构建缓存；自行重建改变二进制后该元数据会过期，
评估工具会拒绝旧身份，不能把旧验收结论套用到新候选。

原 run_baseline.sh 每档三条不同输入，仍适用于初始负载采集；它与上述
同输入重复矩阵不应混算。disconnect_e2e.py 专门复现断连并验证服务恢复。

所有大体积请求、响应、二进制和原始数据库保留在本地忽略目录；Git 仅保存
生成算法、参考摘要和报告。只处理可信本机 evalscope 数据库，其中响应
由工具存为 pickle，不应使用本脚本解析外部不可信数据库。

## 已通过 E2E 后的时间线诊断

仅对已经按 EVALUATION.md 接受的同一二进制执行；不是前置测试，也不替代
五档正式性能验收。profile_http.py 校验既有五档 HTTP/输出记录与二进制
SHA，加载后才开启 Nsight，每档重放一条同输入 256 输出请求并核对摘要。
每条独立 trace，结束服务后导出 SQLite；失败保留证据。需要本机 nsys。

```bash
python3 tools/evalscope/profile_http.py \
  --accepted-run .q4t-work/e2e/position-metadata-20260920/performance \
  --model-dir "$Q4T_MODEL_DIR" \
  --output .q4t-work/e2e/my-http-timeline
python3 tools/evalscope/analyze_http_trace.py .q4t-work/e2e/my-http-timeline
```

--lengths 可指定诊断子集，不能把子集称为完整五档；profile 结果带采集开销，
不得回填 performance_reference.json。分析依赖当前单流普通路径的 logits
和 argmax 回读形状，遇到不同结构会报错。GPU busy 使用区间并集；kernel
累计时间、重叠 API 等待与 GPU 窗口不能相加。详见
[HTTP 时间线报告](../../docs/HTTP_TIMELINE_2026-09-20.md)。

## 输出上限边界 E2E

run_acceptance.py 的 --mode limits 使用固定 1024-token prompt，分别验证
流式与非流式的 max_tokens=1/2/8。先用 --binary 保存旧版结果，再以
--reference 指向该 results.json 重放候选，要求实际输入/输出长度、length
结束、请求 stream 字段和生成文本一致。非流式显式传 --no-stream；
evalscope 默认 stream=true，不能靠省略 --stream 来关闭。

此模式用于输出边界，不替代固定质量题与五档性能矩阵；不把非流式的
TTFT 字段或单次短输出结果解释成 decode 吞吐。

固定 limits 参考已保存为 fixtures/limits_reference.json，来自已接受的
位置元数据版本（SHA dbefb9dd794ebb1da71471d19709b3fb1dbec6415f6f8e47994b124ddb3401a8）
真实流式/非流式重采；可直接以 --reference 使用，不需要每次重跑旧二进制。
它只保存边界输出，不作为性能参考。


## 模型原生聊天模板 HTTP 对照

`run_chat_template.py` 启动单个服务，通过本目录的 evalscope 发送21组
原生prompt→messages→原生prompt，共63条请求；不会预先探测HTTP。
输入模板与分词器摘要固定，服务MTP关闭，greedy、单流；保存实际请求、
原始数据库、输出全文、usage与finish。已有服务运行时拒绝启动。

```bash
python3 tools/evalscope/run_chat_template.py \
  --model-dir ~/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --output .q4t-work/e2e/chat-template-replay-<唯一标识>
```

21组覆盖文本、多轮、思考设置、工具输入格式及数字序列化。11组有
独立答案，另10组只检查与原生模板差分一致，不代表任务答案正确。
该工具不能替代五档长messages性能、非法输入或视觉门禁。模板默认
启用思考；关闭思考需传`chat_template_kwargs.enable_thinking=false`。
思考设置与MTP开关相互独立。工具输入渲染不意味着输出已经实现
OpenAI结构化tool_calls解析。裸prompt由调用方负责模板。

仅当该版本完整HTTP E2E门禁通过后，才可做模板字节细核对：

```bash
g++-14 -std=c++23 -O2 -Wall -Wextra -Iinclude \
  tools/verify/verify_chat_template.cpp src/server/chat_template.cpp \
  src/io/json.cpp -o build/verify_chat_template
build/verify_chat_template tools/evalscope/fixtures/chat_template_cases.jsonl
```

字节参考由模型自带Jinja模板生成并冻结，来源与摘要见同目录metadata。
字节检查通过不替代HTTP或性能接受，测试失败不得重写参考迎合实现。

## 跨会话 GPU 独占（2026-10-16）

完整验证阶段由同一guard持锁，不在子任务间释放：

```bash
python3 tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/my-phase/lease -- python3 .q4t-work/my-phase/run_phase.py
```

锁位于git common-dir所属仓库的`.q4t-work/gpu-exclusive.lock`，不同
worktree共享。flock为占用事实，不根据文件文字判定空闲、不手动删锁。
启动前与运行中轮询/proc及计算进程（默认NVML，显式可选CLI）；
冲突/查询失败记录
ENV_ABORT并返回75，只清理自有进程组及经父子链/start_ticks核实的
自有后代（包括setsid后代），使用pidfd核对身份，不停止冲突方。
命令失败记录COMMAND_FAILED，不能混同环境失败或模型性能失败。
exit.json/events.jsonl保留结果；成功仅表示命令成功且未检测到冲突。
轮询不能排除短于采样间隔、图形或驱动未列出的负载，不承诺绝对独占。

编排注意：当前进程规则保守匹配名称以q4t开头且包含test的可执行文件，
包括q4t_host_tests，即便其当前只运行CPU检查。2026-10-20加载验收v1
因本会话在lease外并行运行host清单而ENV_ABORT。此类检查应在GPU
阶段前完成，或作为同一guard的子进程顺序执行；不要重命名binary
规避监控，不在GPU阶段外启动并行验证。该事件不是NVML查询失败。

### Jetson Thor监控职责与本机能力（2026-10-20）

直接NVML现为默认后端；也可显式指定：

```bash
python3 tools/evalscope/gpu_exclusive.py --compute-backend nvml \
  --output .q4t-work/my-phase/lease -- python3 my_stage.py
```

每次查询使用独立Python worker，通过pynvml调用init/count/handle/
compute-v3/shutdown，不启动nvidia-smi。正常worker退出且完整协议通过
后才发布PID；新请求随机身份，不缓存上次结果。5秒查询时限，超时
杀死自己的worker并等待最多1秒清理；未能回收也记失败，不继续查询。
nvml-queries.jsonl保留每次stdout/stderr、逐API边界及返回状态。错误、
不支持、超时、协议异常和清理失败均触发ENV_ABORT，无自动fallback。
现有flock、PID/start_ticks归属、退出确认及清理策略保留。
默认切换经用户授权，27项默认/显式后端host回归通过；依据是已验证
合同和可诊断性，不是已证明负载下更可靠。没有静默后端降级。

7项query host合同、26项guard host回归通过；原guard保护下，4字节
CUDA上下文启动前/存在时/退出后三次直接查询均成功，正确发现PID及
确认退出。无kernel、模型或HTTP。详见
[计划和结果](../../docs/NVML_MONITOR_PLAN_2026-10-20.md)。这不证明
历史负载下超时已解决，也不证明性能优于CLI。相同NVML/驱动仍可能
阻塞。随后默认后端完成约65.43分钟真实质量/取消/两模式五档阶段，
3595次查询全部OK、最长0.233秒；完整阶段正常关停，仍不据此证明
历史超时根因或一般可靠性。见[本轮结果](../../docs/INFERENCE_METADATA_RESUME_RESULT_2026-10-20.md)。

本机为L4T R39.2.1。轻量只读采集运行tegrastats 3.2秒、取得3条记录，
无模型加载或HTTP请求。内存、CPU、温度、供电轨数据可用；本次输出
没有GPU利用率/频率字段或计算进程PID。缺字段不解释为GPU空闲。
`/proc/driver/nvidia/clients`不存在，不作为可用竞争检测后端。
证据见[能力记录](../../docs/evidence/jetson-monitor-capabilities-20261020/result.json)。

职责分开处理：系统遥测优先用tegrastats；计算进程竞争检查由guard
单独承担；Nsight仅按具体分析需要使用。当前guard默认用直接NVML
PID查询，可显式--compute-backend nvidia-smi切回CLI，失败规则未放宽；超时是监控通道失败，不能
单独证明模型/GPU计算故障。tegrastats不能凭资源使用率替代PID归属。

Thor不同于早期Jetson：官方说明NVML支持Thor及后续L4T平台，Thor
文档列出nvidia-smi dmon的部分支持字段。不能由此推断所有NVML字段
或本机compute-PID查询都可靠。后续后端选择需验证本机接口、权限、
已知竞争进程的可见性和失败行为，再决定是否替换硬依赖。
参考：[CUDA for Tegra](https://docs.nvidia.com/cuda/cuda-for-tegra-appnote/)、
[Thor GPU管理工具](https://docs.nvidia.com/jetson/archives/r38.2.1/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonThor.html#gpu-management-tool)。

默认guard自己持锁。显式`--lease-fd`借用同一lock inode的已排他持锁FD，
仅关闭自身副本，退出记录external_cleanup_pending，外部持锁者负责
恢复与释放；不把guard退出视为外部恢复完成。锁旁存在
`.recovery-required`标记（包括悬空符号链接）时拒绝启动；只能由持锁
恢复流程确认配置恢复后清除，不能为运行测试直接删标记。本工具不
修改GPU频率，专项调频/布局/计数工具已归档，不作为日常验收入口。

## HTTP trace输入身份

profile_http.py要求同binary的完整五档HTTP输出记录，按记录中的实际
输入token数核对profile请求；analyze_http_trace.py亦使用actual input，
不把名义档长当实际长度。`--diagnose-regression`仅记录诊断标签，不
绕过binary、五档和输出门；HTTP完成不等于性能回退已获接受。
trace时间不替代E2E验收，profiler事件不完整时不能推导完整因果关系。

已有验证范围、原始身份及归档恢复见
[主线收敛记录](../../docs/MAIN_CONVERGENCE_2026-10-17.md)。


## 性能决策账本（2026-10-20）

非性能优化业务推进允许小幅波动，性能优化仍按规范评估；先按任务
真实目标分类，不能仅凭账本engineering字段放行性能优化。不要把
已测的统计不足重新标成
未测/失败，也不要在用户已接受同一身份后反复要求确认。具体规则见
[性能决策协议](../../docs/PERFORMANCE_DECISION_PROTOCOL.md)。
`performance_decision.py <ledger.json>`只检查已审计账本的状态与
下一步，不跑GPU、不验证统计结论、不颁发部署许可。host回归：
`python3 -B -m unittest discover -s tools/evalscope -p test_performance_decision.py`。
