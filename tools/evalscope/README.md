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

显式 `--mtp` 才启用单流 MTP；省略时仍传 `--no-mtp`。两组使用不同
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

`--mtp` 同样适用于 performance 和 limits 模式。启动后记录并验证
实际 capabilities 与 capacity：MTP 开关必须符合请求，媒体关闭，
预算启用且可行，实际容量保持 1/8192/208896；预算缩小容量会直接拒绝。
启动通过只证明加载状态，每个 MTP 响应还必须有实际 HTTP ID 对应的
`[q4t][decode_path]` 终态日志，明确 `mtp_multi_b1`、正数 `mtp_steps`
与 `fallback=none`。服务停止后统一核对终态日志，防止最后一个 SSE
先于日志写入导致误判；证据保存到 startup-mode.json/request-modes.json。
旧主线普通 decode 可以没有新终态日志，但仍须证明启动时 MTP 关闭；
这种情况明确记为 `legacy_plain_startup_only`。

仅输出上限为 1 或首次 EOS 导致的 `prefill_only` 可以没有 MTP step，
要求实际输出不超过 1。进入过 MTP 后为上下文边界转普通尾部时，日志
独立保存 `plain_tail_tokens`，不能隐去尾部工作；整个请求实际普通
decode 或任何加载/初始化 fallback 都不能冒充 MTP 验收通过。故障
回退本身是否正确须由单独的边界专项证明。MoE trace 与 `--mtp` 组合
不在本入口接受范围。

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

## MTP 同模式取消恢复

`run_request_cancellation.py --scope same-mode-recovery --mtp` 使用同一
新服务的 fresh control，依次验证 prefill 显式取消、decode 显式取消、
TCP RST 和固定 deadline 后的恢复。范围为 S1、208896/8192、纯文本
和当前 `mtp_multi_b1` 路径，共 9 个生成请求；逐次核对文本、usage、
finish、实际路径、请求计数和槽位回收，不声称完整内部状态逐位相等。

输入由 `--quality-run` 的 44K fixture 与 `--performance-run` 的 1K
fixture 提取，历史生成输出不作为恢复参考；禁止 `--reference-run`。
还需指定 `--binary` 和新的 `--output`（位于 `build/` 或 `.q4t-work/`）。
首次执行前须冻结实际输入与二进制身份，规则见
[转正计划](../../docs/MTP_RELEASE_PLAN_2026-10-08.md)。当前尚未执行这组真实 HTTP。

旧 `full` 与 `decode-recovery` scope 保留；后者仍包含普通/MTP 的
跨模式文本比较，其整体失败不能单独用于认定取消造成了状态残留。
