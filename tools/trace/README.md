# MoE 路由采集与检查

受控完整请求采集，默认关闭。仅支持实际 max_seq=1、文本 greedy、MTP关、
媒体关；不改变 scheduler、prefill chunk 或 Router 算法。默认部署已包含
采集功能，具体版本与成本接受见[当前状态](../../docs/STATUS.md)。
本功能不等于专家 offload。

## 使用

在仓库根目录运行；先准备工作负载文件（例如待发送的 requests.jsonl）。
该文件仅作来源标识，不驱动执行；实际输入 token 会另存并核对 SHA256。

```bash
mkdir -p .q4t-work/moe-trace
# 使用默认已安装版本，模型目录保持只读。
./build/q4t serve --model-dir /path/to/model --max-seq 1 \
  --max-prefill 8192 --max-len 208896 --no-mtp \
  --moe-trace-dir .q4t-work/moe-trace/run-001 \
  --moe-trace-workload /path/to/requests.jsonl --moe-trace-max-mib 1024
```

目录必须位于当前仓库 `.q4t-work/` 内，父目录已存在，运行目录必须全新。
检查启动日志 `[q4t][trace] enabled full-request`，再发送普通 HTTP 请求。
不支持的模式、配额不足或采集池OOM会明确报告 disabled，业务继续运行，
不能把这种运行当成采集通过；其他CUDA初始化错误则使启动失败。
不能同时使用旧 `Q4T_MOE_DUMP`。

请求完成后后台生成 `request-N.bin`、实际小端 uint32 输入 `.tokens`、
关联 HTTP ID 的 `.json`；取消/失败也保留自己的终态，内部 N 不复用。
未完成或采集故障保留 `.partial`。正常停止服务后才发布运行级 complete。
完整轨迹工具只读输入：

```bash
python3 -B tools/trace/analyze.py \
  --directory .q4t-work/moe-trace/run-001 \
  --binary ./build/q4t --checker /path/to/q4t_router_trace_check \
  --output .q4t-work/moe-trace/run-001-analysis.json
```

检查器可由完整构建或无 CUDA 的 `tests/host` 构建获得。分析核对二进制、
模型 index/config 和 workload 副本摘要、实际启动命令、相关环境、输入
token 摘要、帧、层、位置与终态。command.bin/environment.bin保留原始
NUL分隔字节；环境只包含Q4T_*、LD_PRELOAD/LD_LIBRARY_PATH及CUDA_VISIBLE_DEVICES。
模型 index 摘要是索引身份，不是全部权重内容哈希。完整验收还需保留启动
命令、环境、模型只读约束与 HTTP 记录；轨迹本身不是数值正确性 oracle。

输出为每请求计数、逐层/阶段的专家频次和同一批样本的事后 top-N 覆盖。
取消/失败请求列出但不计入汇总覆盖；这些数字不是留出热点、缓存命中率
或 SSD 吞吐。未完整停止、空采集、缺文件或摘要不符一律拒绝分析。

## 资源与失败合同

四个固定 pinned 槽位、一个独占 device 缓冲，最大各16 MiB。8192 chunk、
48层、top-k=10 时分别为60 MiB pinned和15 MiB device，启动日志与manifest
记录实值；超过池上限禁用采集，不调整模型容量。最多1024个已接纳请求，
逻辑文件字节配额默认1 GiB，可设1–4096 MiB，包含索引/输入和元数据预算。
索引本身约35 MB，过小配额会在初始化时禁用采集。

层内在同一 stream 上复制实际 ID；forward 末尾 D2H 使用原有受检查同步，
完成之后才能交给 writer 或复用 device 缓冲，未完成槽位隔离到安全释放；
最终drain仍失败时保留采集池到进程退出，不提前释放pinned内存。
请求/调度线程不等待 writer、不写盘、不编码帧；仅请求起点复制实际输入。
writer 负责 SHA256、CRC、ID校验、编码和文件发布。队列满、配额耗尽、
写盘失败即停止本次运行的后续采集，标记 incomplete，不静默丢样后续写。
CUDA错误仍进入原服务失败/不健康路径。关闭态不建池、不启动writer。

这是受控诊断模式，启用开销须看 HTTP 对照，不能宣称长期零开销。后台
普通文件 IO 仍可能阻塞于文件系统，正常关闭会等待 writer 退出；尚不提供
故障文件系统下的硬超时保证、长期采样、轮转或自动恢复。未实现专家缓存。

## 验证入口

`ctest --test-dir build/public-host --no-tests=error --output-on-failure`
检查host合同与49项独立格式/CLI样例；`test_analyze.py --checker ... --output ...`
检查来源绑定和不完整样本反例。`run_capture_checks.py`接受候选binary/checker、
既有quality/performance证据和全新output，实际运行取消、inline/fallback、
不支持模式、配额、写盘/关闭失败、队列压力、池分配失败、停止/崩溃及
采集CUDA故障；测试shim不会链接进runner。`--case NAME`可重复选择受影响
用例，未知、重复或空名称退出非0；不传则执行全部15项。
这些检查与HTTP质量、五档性能分别记录，不将合成数据当作真实采集验收。

## 固定容量离线缓存回放

`replay.py --plan PLAN.json --checker CHECKER --binary BINARY --output OUTPUT`
只读完整采集包；复用analyze与C++检查器，拒绝损坏、空集、失败请求、
校准/评估重叠、未知来源与不足64次decode。输出不能覆盖，限build/.q4t-work。
计划schema及本轮固定划分见 `replay-plan-20260928.json`；输入路径相对仓库根。
公开仓无私有轨迹时明确失败。`test_replay.py`是无GPU合同入口；
`verify_replay.py --results OUTPUT`从原始轨迹独立重算全部冷decode静态/LRU。

合同：48层各自固定32/64/128/256槽、512专家、top-10；当前模型以外拒绝。
静态排名仅用校准请求前64次decode，频次降序、ID升序破同分，启动一次
填满并计费；LRU从空开始、按组时间戳更新、同时间戳按ID升序淘汰。
所有命中先用组前状态计算；decode整组保护到消费结束，不把组内加载算命中。
需求路由已出现时才允许补载，本工具没有预取，也不把host采集延迟视为零。

prefill保留实际forward块边界，按同层块内专家并集一次消费；每个缺失
专家只计一次加载。并集能放入缓存时LRU整组接纳；超容量时保留原缓存，
按专家组流过staging，不接纳、不刷新LRU。静态所有miss均流过staging。
这是未来执行器的显式假设，不是现有grouped kernel支持分批换权重的证明。
预留跨层串行共用10个staging槽：decode可同时放10个miss；prefill逐专家
完成该块全部相关token的gate/up/down后再复用，不在中途淘汰组内在用权重。

三种模式：cold_decode每请求重置并跳过prefill（隔离实验）；prefill_reset
每请求重置、真实prefill后保留；continuous按计划顺序跨请求保留，cohort
之间重置。每个请求仅回放前64次decode，64之后是**反事实截断请求**，
不把未回放的后63/191次decode充当历史。冷LRU的窗口前部就是有费预热；
静态初始填充单列，比较总量必须加回。没有免费预热、校准状态移植或未来路由。

每专家payload按moe_weights.h/swizzle.h为2,764,816字节，含packed、
atom padding、四个FP32 scale；假设256字节对齐slot为2,765,056字节。
逻辑加载按整个slot计，包含240字节padding，不等于磁盘文件读取字节。
总路由预算=(48×容量+10)×slot，两策略完全相同；共享专家与其他常驻
权重在路由预算外，硬件可用性还必须扣除这些与KV/状态/workspace/IO/系统余量。
结果不声称硬件容量认证；旧122/59 GB不能当缓存容量，详见分析报告。
输出逐请求/层/阶段的选择命中、并集命中、整组全命中、缺失直方图、逻辑
加载和峰值驻留槽字节；所有组/字节是逻辑值，不折算吞吐或NVMe物理读取。

## 请求完成边界的在线影子统计

`shadow.py`是独立观察进程，只消费采集器原子发布的request-N.bin，服务继续
运行时按请求序号处理。没有runner接入、逐层及时可见性或权重控制；晚一个
请求看到路由，不能据此证明补载能够及时完成。记录处理耗时、积压峰值和RSS。
完整decode不再截64步；32/64槽、静态/LRU、prefill_reset/continuous并行统计，
沿用上述prefill bypass与付费填充合同。取消/失败请求的已提交前缀仍算历史，
未提交forward不更新状态，请求终态单列；不得悄悄跳过失败后继续当完整流。

```bash
python3 -B tools/trace/shadow.py --directory .q4t-work/NEW-capture \
  --binary build/q4t --checker /path/to/q4t_router_trace_check \
  --calibration .q4t-work/frozen-calibration.json --output .q4t-work/NEW-shadow
```

在服务采集开始前启动，服务正常停止后才输出complete。已完成的旧run拒绝称为
在线观测。校准文件为不超过1MiB的JSON，含48×512完整rankings及
model_index_sha256；须从已验证校准集导出并保存原来源摘要，不能从新评估
请求反向训练。默认最多64请求、每bin最多256MiB、请求统计输出最多128MiB（另有status/校准元数据）、
等待最长7200秒、轮询250ms；显式参数只能缩小请求数/期限。解析器自身的
frame上限16MiB。单个受限文件的解析/IO不可抢占，deadline非故障文件系统硬超时。
源失败/缺口、损坏、超限或超时会非0停收并保留status.complete=false；不发
服务信号、不等待服务配合。已发布输入应保持只读；需要新观察时用新输出目录。

`test_shadow.py --checker ... --output build/shadow-contracts`运行直接合同；
`verify_shadow.py --directory CAPTURE --output SHADOW --checker ... --binary ...`
正常停机后重新验证完整来源，再用独立有序列表算法重算全部prefill/decode、
静态/LRU及两种保留模式。`run_shadow_study.py`运行固定编写材料、任务切换
和三个多轮对话对，保存全部HTTP请求/响应；不把它称作生产样本或语义质量基准。
影子开启的质量与五档成本由tools/evalscope另行验收，不能从本工具逻辑字节推吞吐。

## EvalScope 多场景初步采样

`run_evalscope_scenarios.py --output NEW --model-dir MODEL --checker CHECKER
--calibration FROZEN`使用现有EvalScope发送24条编写请求：六类任务各四种
材料规模，按规模轮换场景，单流、greedy、关闭思考/MTP、输出上限128。
冻结requests.jsonl后再启动服务，保存EvalScope数据库、输出全文、命令、
来源身份及逐请求路由/影子JSON；每次独立目录，不覆盖、不续写旧run。
默认binary为build/q4t，端口18085，可显式指定。材料是合成订单与日志，
不是生产流量；HTTP成功不等于答案正确，截断输出不按完整答案计分。

采样结束后运行`analyze_evalscope_scenarios.py --directory NEW --checker CHECKER
--binary BINARY`，完整验证来源并独立重算所有层/阶段/策略，再核对HTTP ID，
输出analysis/requests.csv、summary.json和文件摘要。分析目录不能覆盖。
连续模式沿全run实际顺序保留；按场景汇总是该顺序的切片，并非各场景独立
冷启动。校准始终使用旧冻结排名，不用新请求训练。命中率先求和再相除。
这轮仅扩展路由材料，不替代正式质量基准或五档同输入性能接受结论。

已封存的24条多场景轨迹还可用`replay_scenario_capacities.py --directory RUN
--output NEW --checker CHECKER --binary BINARY`做离线32/64/128/256槽扩展。
它校验原analysis/bindings.json、完整来源与冻结校准，逐需求组用独立有序
列表算法核对Cache，再要求32/64逐请求计数与原影子结果完全一致。输出
逐请求CSV、容量汇总与核验JSON，不改变在线观察器的32/64容量或其成本。
所有容量保持相同完整请求顺序与prefill合同，不重新发送HTTP。

`replay_hybrid.py --directory RUN --baseline CAPACITY_RUN --output NEW
--checker CHECKER --binary BINARY`比较64/128/256槽、固定区占0/25/50/75/100%，
余下LRU；排名只用原冻结校准。prefill扣除固定命中后，剩余并集放不入动态
区则bypass，不污染动态顺序。初始化固定区计费；同时比较每请求重置和连续
保留。逐组独立列表核对，0/100端点匹配原纯LRU/静态；test_hybrid.py提供
直接合同。结果仅离线，见docs/MOE_HYBRID_2026-09-28.md。

## 专家ID分布（不做缓存回放）

`distribution.py --plan tools/trace/distribution-plan-20260928.json --output NEW`
核验固定计划的常规完整轨迹，分别统计prefill/decode、48层全部512专家。
依赖NumPy；输出逐请求NPZ、逐批/类别/场景/长度分组NPZ、主聊天样本密集
experts.csv、逐层layers.csv、完整身份和重复标记。只消费已提交forward；
非成功请求保留在逐请求数据，聚合排除。相同输入token在同类别/模型身份
下按首次成功记录生成unique_input视图，all和原批次仍保留；不同路由的
重复会显式标记，不隐去差异。模型/类别不能凭专家编号混合解释。

频次为选择次数，block_presence为块内出现一次，request_presence为阶段内
出现一次；等权先逐请求归一化，无阶段的请求不进入该阶段分母。Top-N仅
为同样本描述性集中度，排序相同计数时ID升序；零频不补入稳定性名单。
半段按阶段实际行数二分，名单不足64活跃专家的层不参与Top64重合均值。
未观察到不能称死专家，场景相关不等于因果语义分工。

`summarize_distribution.py --directory NEW`重算分组并生成readout/汇总、
场景表和跨请求名单重合；输出不能覆盖。`test_distribution.py --checker
CHECKER`运行直接合同，也可在末尾指定unittest用例名称只重验受影响项。
本轮来源、排除项、口径及结论见docs/MOE_DISTRIBUTION_2026-09-28.md。
公开仓不包含私有轨迹；缺源即失败。没有发送模型HTTP或评价缓存策略。

`expert_probabilities.py --directory DISTRIBUTION_RESULTS --output NEW
--population authored:unique_input`以token路由次数为分母导出每个专家进入
同层top-10的边际经验概率（全512及最高10位、CSV/Markdown）。每层总和10，
不是全部选择份额的总和1；不等于固定十位同时出现的概率。默认聊天去重
组，可选择populations.json中的其他组；输出不覆盖原数据。

`top_expert_sets.py --directory DISTRIBUTION_RESULTS --output NEW --top-n 30`
将每层同样本Top-N名单与每个token实际top10比较，输出各专家入选概率、
选择覆盖率、平均重合个数、至少一个/全部十个概率和0–10个重合计数。
后两概率从原始轨迹直接计算，不假设专家独立；prefill这里按token而非块。
可用--population选择分组。全部属于同样本描述统计，不是缓存预测。
