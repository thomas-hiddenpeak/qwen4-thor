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
