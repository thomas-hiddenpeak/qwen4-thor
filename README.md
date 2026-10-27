# Qwen4-Thor

在 NVIDIA Jetson AGX Thor (SM110a, Blackwell) 上为
[Qwen3.8-Flash-Next](https://modelscope.cn/models/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream)
构建的原生 C++23 (host + device, CUDA 13.3) 推理引擎。

核心特性:**PLE SSD Stream** —— 51.2 GB FP8 n-gram 哈希嵌入查找表 (PLE,
技术报告称 N-gram Embedding Layer, 仅 Layer 2 一层) 不主动整表常驻,通过
Linux `io_uring` 按需读取本地 NVMe 页面，避免常驻约 47.68 GiB 的整表。
文件页缓存、页池、pinned staging 和 GPU scratch 都占统一RAM；
读取是否被已提交GPU工作掩盖取决于实际调度，不保证固定重叠比例。

## 能力概览

- **文本推理**: 48 层完整 forward (36 linear_attention + 12 full_attention),
  MoE 专家使用 NVFP4 W4A4，其他路径含 BF16/FP32；Paged KV cache、greedy 生成
- **多模态**: 27 层 ViT, 图像 + 视频输入 (OpenAI `image_url` / `video_frames`
  base64 接入), 3D MRoPE；当前版本默认拒绝媒体，须显式
  `serve --allow-media`进入未验收实验路径（部署身份见STATUS）
- **MTP 推测解码**: 默认开启 T>1 batch verifier（k=3），`--no-mtp` 回退；
  默认使用完整草稿 head，`--mtp-subset-head DIR` 显式启用已验收的
  65536 行 BF16 独立草稿 head；当前合同、质量与性能边界见 STATUS
- **历史 suffix 草稿**: `--mtp-suffix` 显式启用固定 k3 历史匹配，适用于
  greedy 文本，可与两种草稿 head 配合；支持单流及已验收的1长＋3短agent
  配置。重复场景净收益、保护负点与本次用户接受范围见
  [长短agent接受决定](docs/MTP_SUFFIX_AGENT_ACCEPTANCE_2026-10-27.md)
- **完整会话缓存**: 显式`--session-cache-dir DIR`加请求`session_id`，保存
  完整目标/草稿状态到有界SSD缓存；工具等待期间释放执行槽，槽位被其他
  请求使用后可恢复原前缀。同进程长会话范围与成本见
  [会话缓存交付](docs/SESSION_CACHE_RESULT_2026-10-27.md)
- **专家SSD卸载**: 默认全部专家驻留，非零`--moe-resident-slots`
  显式启用配置的device槽位与SSD后备读取。Thor主机内存和GPU共用
  DRAM，CPU缓存不构成容量卸载；中转、缓存与工作集均计入统一RAM。
  与PLE是不同机制，offload研究不等于默认MTP验收，见
  [SSD offload合同](docs/SSD_OFFLOAD_CONTRACT.md)
- **连续批处理**: token 级打包、多请求调度；已完成有界槽位和生命周期
  验证，不能据此保证所有路径均不存在状态污染
- **长上下文**: 流式 top-k QSA；当前正式单流评估覆盖 1K、4K、8K、44K、
  200K，历史 262K 运行记录不代替当前发布验收
- **资源管理**: 启动内存预算、auto-length、运行时 preflight，以及由
  配置/张量形状推导的设备预留与所有权核对；受测热缓存启动已完成
  [有界可靠性交付](docs/STARTUP_DELIVERY_2026-10-21.md)。不保证任意
  配置或异常输入不 OOM，媒体解压等预算仍待完善
- **HTTP API**: OpenAI 风格 chat/completions 流式/非流式接口，以及模型、
  健康、指标和取消接口；仅 greedy，不代表支持全部 OpenAI 参数语义
- **验证边界**: 已有数值缺陷修复、状态/故障测试和五档 HTTP 性能参考。
  [首版私有文本部署](docs/RELEASE_TEXT_V1_2026-09-28.md)已验收；
  公网多租户与业务SLA另行验收。当前结论见 [STATUS](docs/STATUS.md)，
  已知缺陷与未覆盖项见 [完善度审计](docs/PROJECT_READINESS_REVIEW_2026-09-27.md)

## 目标模型

- 架构: `qwen4_exp` (48 层: 36 linear_attention + 12 full_attention)
- MoE: 512 专家/层, top-10 路由 + shared expert, NVFP4 W4A4 量化
- 注意力: GQA 24Q/2KV, head_dim 256, MRoPE (interleaved)
- PLE: 每 token 16 次确定性查找, FP8 行 160 字节, 51.2 GB sidecar
- MTP: 1 层 full_attention 推测解码
- 视觉: 27 层 ViT (图像/视频输入)
- 模型配置最大上下文: 262144 token；当前正式 HTTP 矩阵最大名义档为 204800

## 硬件目标

| 项 | 值 |
|---|---|
| GPU | NVIDIA Thor, SM110a, 20 SM |
| 内存 | 122 GB LPDDR5X 统一内存 |
| CPU | 14 核 ARM Neoverse V3AE |
| 存储 | NVMe SSD |
| 工具链 | CUDA 13.3, CMake 4.0+ (pip 装 4.4.3), g++-14 (apt) (aarch64) |

## 构建

```bash
# 要求: CMake >= 4.0 (CMAKE_CUDA_STANDARD 23 仅在 4.x 映射 nvcc --std=c++23)
#   pip install --user --break-system-packages cmake==4.4.3
# 要求: g++-14 (nvcc 的 --std=c++23 需 host 编译器支持, GCC 13 会静默忽略)
#   sudo apt-get install g++-14 pkg-config liburing-dev libicu-dev
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_CUDA_HOST_COMPILER=g++-14 \
  -DQ4T_CUDA_ARCHITECTURES=110a
cmake --build build --parallel

# 开发测试（默认SKIP也非零退出；发布范围与入口见tools/release）
python3 tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/manual-tests/lease -- \
  ctest --test-dir build --output-on-failure
```

## 使用

```bash
# 版本与硬件探测
./build/q4t version
./build/q4t probe

# 独立 CLI 生成路径；不属于下述 HTTP MTP 验收范围
./build/q4t generate "用一句话解释统一内存。" --max-tokens 32

# 已验收的 HTTP 单流配置；默认 MTP k=3
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 1 --max-prefill 8192 --max-len 208896
# 普通 decode 回退：在上述 serve 命令末尾添加 --no-mtp
# 显式启用历史 suffix：在上述 serve 命令末尾添加 --mtp-suffix

# 显式使用已交付的 65536 行 BF16 独立草稿 head；仍为单流 MTP k=3
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 1 --max-prefill 8192 --max-len 208896 \
  --mtp-subset-head .q4t-work/sha2-startup-20261023/delivery-package/artifact

# agent：1个长槽＋3个短槽；完整head，显式开启suffix
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 4 --max-prefill 4096 --max-len 208896 \
  --seq-capacities 208896,65536,65536,65536 --mtp-suffix
# 子集head：在该命令末尾添加上面的 --mtp-subset-head DIR
```

以上命令从仓库根运行。子集示例使用本机保留的交付产物；该目录包含
`head.bf16`、`token_ids.bin` 和 `manifest.json`，不随 Git 克隆下载。
其他机器需准备与模型及 tokenizer 匹配的产物，并将参数替换为实际目录；
产物生成方法和固定候选边界见[转换工具说明](tools/mtp_subset/README.md)。

`serve` 参数默认值来自 `ServerOptions`，不是上述示例的显式配置：

| 参数 | 默认值 / 行为 |
|---|---|
| `--host` / `--port` | `127.0.0.1` / `8000`；host 接受数字 IPv4 |
| `--max-seq` | `1` |
| `--seq-capacities` | 默认等长槽；显式逗号列表为每槽输入＋输出容量，须与max-seq/max-len匹配，预算不足时拒绝；推荐agent布局见上例 |
| `--max-prefill` | `0` 表示使用模型默认 `2048`；可显式设至 `8192` |
| `--max-len` | `0`，默认预算开启时自动推导有效长度 |
| `--max-tokens` | 请求省略输出上限时使用 `256` |
| `--mtp` / `--no-mtp` / `--mtp-k` | MTP 默认开，k 默认 `3`；serve 接受 k=1..16，验收仅覆盖 k=3 |
| `--mtp-subset-head DIR` | 默认不启用，草稿使用完整 head；指定产物目录启用固定 65536 行 BF16 子集，要求 MTP 开启且 k=3 |
| `--mtp-suffix` | 默认关闭；要求文本、greedy、MTP k=3、有效max-len≤262144，可配合完整或子集 head；交付覆盖单流及上述4槽布局 |
| `--session-cache-dir DIR` | 默认关闭；另须请求传session_id才缓存；同进程文本k3、显式槽位，8项/24GiB SSD/30分钟TTL；不组合媒体、trace或专家offload |
| `--request-deadline-ms` | `1200000`（20分钟）；含等待槽位和生成，显式范围1000～10800000；客户端超时需匹配 |
| `--allow-media` | 默认不启用，显式启用实验媒体路径 |
| `--mem-fraction` | `0.90`，启动内存预算比例 |
| `--moe-resident-slots` | `0`，所有专家驻留；非零启用按需驻留 |
| `--startup-plan` | 默认`auto`；`off`保留旧分配路径，`required`要求支持并启用预留计划 |

`--model-dir DIR` 可覆盖代码中的本机默认模型路径。完整参数解析见
[src/server/server_options.cpp](src/server/server_options.cpp)。当前 main 使用
T>1 batch verifier；`--mtp-verifier` 已移除，sequential 实现在历史提交
`7f6a69c` 中保留。当前交付与基线见 [STATUS](docs/STATUS.md)。

### 独立草稿 head 与启动校验

`--mtp-subset-head` 是进程启动参数，不是 HTTP 请求参数。不能与
`--no-mtp` 或非 3 的 `--mtp-k` 同用。产物缺失、摘要或映射不合法时
明确启动失败，不静默改用完整 head。

移除该参数并重启即可回到完整草稿 head，MTP 仍开启；若要回到普通
decode，移除该参数后加 `--no-mtp`。这些切换不需要更换程序。
回退整个程序版本另见[两级回退包](docs/MTP_SUBSET_MAIN_MERGE_2026-10-23.md#两级回退包)。

子集只裁剪草稿投影，主模型完整 head 继续驻留，新增权重和映射
320.25 MiB；加载中转和文件页缓存另占统一 RAM。BF16 保持，但草稿
候选和投影舍入可能变化，不承诺任意输入输出无损。受测收益、质量
和资源边界见[有界验收](docs/MTP_SUBSET_RESUME_2026-10-23.md)。

SHA2 校验加速无需启动参数：支持时自动使用 ARM SHA2 指令，否则走
软件实现；源权重、tokenizer 和派生产物的完整 SHA-256 校验保留。
它减少子集加载时的校验耗时，不省略校验，也不改变 head 的默认选择。
实测启动成本见[SHA2 验收](docs/SHA2_STARTUP_2026-10-23.md)。

### 固定 k3 历史 suffix 草稿

在上面的完整 head 或子集 head 服务命令末尾添加 `--mtp-suffix`，即可
尝试从本请求已提交历史中取三个草稿 token。选定匹配至少32 token，
须通过数字格式过滤且首候选与已计算的d0一致；否则继续使用神经草稿。
候选使用完整词表的全局ID，不要求都属于65536行子集；仍经过目标模型
四行验证及接受前缀状态更新。索引不跨请求保存。

移除 `--mtp-suffix` 后重启即关闭该功能，不改变 head 选择。它不能与
`--no-mtp`、非3的k、`--allow-media` 或超过262144的容量
同用，不支持的组合启动失败。输出或上下文不足时沿用普通decode尾部。
`generate` 不接受该开关，其验收范围不随HTTP扩展。

每请求索引动态内存上限2MiB，共享按需token属性缓存上限256KiB，另计
对象、tokenizer临时分配和allocator成本。1长＋3短重复代码/文本的
E2E吞吐收益为完整head约1.98%/1.83%、子集约1.12%/1.05%；decode收益
分别约5.44%/5.22%、2.99%/2.85%。子集未达原3%decode目标，用户接受
本次实测收益与小幅保护负点；统计总体非劣未建立。旧单流短重复收益
属于旧程序身份的证据，不替代这些并发长输入测量。

上述4槽配置按输入加最大输出选择最小可用合适槽：最多一个活动请求
超过65536 token预算，其余三个最多65536（输出也占容量）。prefill暂存
以4096 token分块消费。执行槽仍为4个，解析/排队/生成请求总数上限32；
16/32突发只验证了短请求，不保证32个长请求的完成时限。单请求body
上限16MiB、全局body预算48MiB，超限仍会拒绝。长prefill会影响其他请求
TTFT，客户端须为排队留足超时。使用、回退及健康检查见
[长短agent交付记录](docs/MTP_SUFFIX_AGENT_DELIVERED_2026-10-27.md)。

### 完整会话缓存

为需要跨工具等待复用的长会话显式启用缓存：

```bash
install -d -m 700 .q4t-work/agent-session-cache
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 4 --max-prefill 4096 --max-len 208896 \
  --seq-capacities 208896,65536,65536,65536 \
  --mtp-suffix --session-cache-dir .q4t-work/agent-session-cache
# 子集head另加：
# --mtp-subset-head .q4t-work/session-cache-20261027/delivery-package/artifact
```

主agent和每个子代理分别生成并保存一个`secrets.token_hex(32)`作为
`session_id`（64个小写十六进制字符）。每次HTTP请求在顶层传这个字段，
同一会话串行复用，不同会话使用不同ID。仍提交完整`prompt`或`messages`，
不是只发新增工具结果。普通短请求可省略ID；只开启启动参数不会自动缓存
未传ID的请求。正常响应结束保存完整前缀并释放执行槽，其他请求使用原槽
后，下一轮可以恢复到任意容量足够的槽。长于65536的请求仍只能使用长槽。

要稳定命中，客户端需保留原始模板文本及生成历史。本轮收益使用预渲染
`prompt`测量；`messages`恢复前也逐token检查，但模板会trim文本、重建
thinking和工具调用。带thinking的整段输出直接放回`content`可能被重复
包裹，结构化接入需正确保留`reasoning_content`、`content`和`tool_calls`。
历史被改写、压缩或模板选项改变时可能未命中并重新prefill。当前没有针对
某个agent SDK的即插即用适配验收。

检查响应的`usage.prompt_tokens_details.cached_tokens`；流式请求需同时传
`"stream_options":{"include_usage":true}`。大于0表示实际恢复的目标前缀
token数，0表示本次重新计算。缓存保存最后一个完整MTP边界，最多4个旧
尾部输出token仍会重算。跨轮状态正确性以独立接受前缀参考和实际HTTP为准，
不承诺任意输入与fresh prefill位级相同。

首版同进程内最多8份快照、SSD总预算24GiB、30分钟TTL；淘汰/过期/进程
退出后正常重新prefill。目录需本用户私有0700、非tmpfs且支持O_DIRECT和
O_TMPFILE，匿名文件不会显示为普通缓存文件。新增RAM预算预留32MiB，
并不释放已分配GPU槽位内存。保存/恢复会占模型锁并影响其他请求等待。
要求纯文本greedy、MTP k=3和显式槽位容量；不组合媒体、router trace或
SSD专家卸载。移除`--session-cache-dir`并重启即可关闭，客户端同时停止传ID。

受测1长＋3短两轮事务（含首次保存、其他请求占槽、恢复）的净耗时降低
27.5%～27.8%，恢复TTFT降低约95%；首轮写盘增加约8.8～9.4秒。数据是两种
head各一个代码/文本描述性配对，不保证任意agent工作流收益。完整结果、
保护负点、统计限制和回退见[会话缓存验收](docs/SESSION_CACHE_RESULT_2026-10-27.md)。

### 其他入口与验收范围

启动计划在`auto`下仅对明确不支持的模式在计划开始前选择旧路径；
元数据、预留或交付失败直接停止。支持范围与本次1K可靠性验证见
[正式交付记录](docs/STARTUP_DELIVERY_2026-10-21.md)，不代表新增长输入、
并发或性能优化结论。

`generate` 是另一套调用路径：输出上限默认 64，默认尝试加载 MTP，
接受 `--mtp`、`--mtp-k`、`--mtp-subset-head DIR`、`--max-prefill`、
`--model-dir`、`--max-tokens`，指定子集时同样要求 k=3，
**目前不接受 `--no-mtp`**；不能将 serve 的参数或验收直接套用到它。

冻结验收范围为本机 Thor、既定 checkpoint、纯文本 greedy、单流 k=3、
`--max-seq 1 --max-prefill 8192 --max-len 208896`，以及上述显式1长＋3短
agent配置。其他 CLI、媒体、并发布局或 k 值不随此矩阵取得验收。
T>1 与普通 decode 的舍入路径不同，遵守
[EVALUATION](docs/EVALUATION.md) 的 N=4 ULP 近 tie 合同；不承诺跨路径
逐位一致。默认监听 127.0.0.1。

性能评估使用 [tools/evalscope](tools/evalscope/README.md) 的真实 HTTP E2E；
bench 不作为性能验收依据。归并方案、共用正确性修复与回退点见
[MTP 归并边界](docs/MTP_MERGE_BOUNDARY_2026-10-15.md)。

`POST /v1/chat/completions` 使用 OpenAI 风格请求体，content 可为字符串或
parts 数组; 图像用 `image_url` (base64 data URL), 视频用 `video_frames`
(base64 帧数组)，仅在 `--allow-media` 下可接入。另有 `GET /healthz`、
`GET /v1/models`、`GET /metrics` 与 `POST /v1/requests/cancel`。

**长上下文注意**: `--max-len` 按需最小 (如 44K 用 49152), 单请求场景
显式 `--max-seq 1`; 未 pin `--max-len` 时按 `--mem-fraction` 自动推导
(auto-length)。预算器可对部分超预算配置执行 CAPPED 回退；这不覆盖
系统其他进程、运行时异常输入或全部主机内存分配。

## 文档

- [AGENTS.md](AGENTS.md) — 开发入口, 面向 agent 的项目导航
- [docs/](docs/README.md) — 开发记录文档体系 (状态、架构、阶段计划、日志)
- [reference/](reference/) — 外部参考项目 (只读, 不参与构建)

## 参考项目

| 项目 | 用途 |
|---|---|
| [qwen35-thor](https://github.com/thomas-hiddenpeak/qwen35-thor) | 同硬件 Qwen3.5 推理引擎, 架构模式参考 |
| [sglang-ssd-stream](https://github.com/garnermccloud/sglang-ssd-stream) | PLE SSD Stream 机制参考 |
| [vllm](https://github.com/vllm-project/vllm) | qwen4_exp 完整实现, MTP/QSA 权威参考 |
| [thor-probe](https://github.com/thomas-hiddenpeak/thor-probe) | 硬件探测方法 |
| [thor-bench](https://github.com/thomas-hiddenpeak/thor-bench) | 硬件性能基线 |

## 许可证

MIT (项目代码)。模型权重遵循其发布方条款。
