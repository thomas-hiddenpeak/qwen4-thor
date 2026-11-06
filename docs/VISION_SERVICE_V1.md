# 视觉服务 v1 使用与运维边界

2026-11-07。固定 Qwen3.8-Flash-Next checkpoint、Thor、greedy、单执行槽。
图片业务已有限接受；视频输入/数值合同通过，但原四个运动方向枚举任务
失败。[后续收口](VISION_CLOSEOUT_RESULT_2026-11-07.md)中明确schema含义的
应用请求四项通过，仅接受该请求方式和有限夹具。全局媒体声明保持 `experimental: true`，默认
不开启媒体。完整原始失败和接受依据见[结果](VISION_PRODUCTION_RESULT_2026-11-07.md)。

## 支持矩阵

| 入口或组合 | v1 状态 | 边界 |
|---|---|---|
| PNG/JPEG 单图、多图 | 有限接受 | 自然物体颜色/位置、表格 OCR、图表、多图汇总通过；不是任意图像准确率保证 |
| 视频帧序列/时间戳 | 输入与数值接受，任务质量未接受 | 2～32 帧/项；正向、反向、奇数和编码视频的 strict 方向枚举均失败 |
| 明确语义的方向schema应用请求 | 四夹具有限接受 | schema同时写入提示，正/反/奇数/CPU采样H.264通过；原请求失败保留，不外推任意视频 |
| CPU 本地视频采样 | 有限接受 | OpenCV/FFmpeg；真实 PTS、明确有界、无网络下载；视频任务仍有上述限制 |
| Jetson 编码 | 单夹具通过 | NVENC H.264，8 帧 384×256；无吞吐、全格式结论 |
| Jetson 解码 | 未接受 | `nvv4l2decoder` 缓冲池分配失败；显式实验后端不自动回退，不作为生产示例默认 |
| JSON/SSE 输出 | 有限接受 | 完整终态和 usage；strict 输出校验后统一发布，不保证逐 token 流式 |
| 视觉工具调用 | 有限接受 | forced strict 参数、真实只读项目资料查询、回填及最终引用；runner 不执行任意工具 |
| 真实项目记录图片与工具核对 | 有限接受 | 实际验收记录渲染表/柱图/多图及真实文件比对；不是自然扫描。完整引用回答实测289token，256预算失败 |
| JSON object/有限 JSON Schema | 语法合同接受 | `json_object` 允许附加键；schema 正确不等于视觉答案正确 |
| preflight | 有限接受 | 共用展开计划、真实 prompt token/输出预算；仅 header/几何检查，不解码像素、不跑模型、不预留额度 |
| TLS/独立 Key、RPM/TPM、公平排队 | 本机回环组合接受 | 展开 token 计费；准备完成后按客户端轮转；单执行槽，无 GPU 时间或延迟公平承诺 |
| 取消/错误/恢复 | 受测路径接受 | 帧/项及 GPU 执行边界协作取消；不是任意 CUDA kernel 抢占 |
| MTP、状态/前缀缓存、suffix、分块、多执行槽、专家 offload | 视觉组合拒绝 | 必须显式选择兼容 profile，不能直接移除 gate |
| 长稳、真实 LAN、持久任务/托管包迁移 | 未验收 | 长稳由用户开展；旧包继续按旧 ELF/协议固定，不自动升级 |

输入限于 user 消息的 PNG/JPEG data URL 或规范 padded base64，
`detail: "auto"`。最多 8 个媒体项，每个视频 2～32 帧。
`video_timestamps` 与帧一一对应，有限且严格递增，范围 0～86400 秒；
省略时按帧序号/24，不能代表任意原视频真实时间。每两帧组成一个时间组，
使用均值时间戳和独立视觉边界；奇数尾帧重复。视频原文件、远程 URL、
音频均不由 runner 接收。具体限制以 `/v1/capabilities` 和 `/v1/config`
为准；媒体限制不是整机物理 RAM 硬限额。

## 本机启动与客户端

可选视觉诊断：新构建程序设置`Q4T_VISION_TIMING=1`后，每个进入视觉
流水线的请求在stderr输出一条`[q4t][vision-timing]`，以request_id关联。
默认关闭，不改HTTP响应；原固定发布包不自动获得该能力。各字段是主机
墙钟毫秒，`forward`包括CPU提交/位置表处理和原有GPU完成同步，
`workspace`含旧workspace释放和表构造/分配，不是纯kernel或纯分配时间。
没有新增CUDA同步。`total_ms`不含HTTP/base64、排队和LLM执行，
`other_ms`保留未覆盖的本地开销；`completed=0`只能用于失败诊断。
PNG/JPEG分别累计，`*_calls`为计时作用域次数（cleanup含空指针释放尝试）。
使用`tools.vision_service.timing_analysis.parse_records`解析并检查分项
和总数；部署前仍按[本轮计划](VISION_TIMING_PLAN_2026-11-07.md)的范围验收。

视觉塔内部诊断另用`Q4T_VISION_FORWARD_TIMING=1`，默认关闭，可独立于
上述墙钟启用。stderr的`[q4t][vision-forward-timing]`记录10段CUDA stream
区间及残余，最大8192个event；只在原完成同步后读取，不逐层同步。
`clock=cuda_stream`包含主机提交间隙，不是各kernel净执行时间；位置表
`position_host_ms`含原有阻塞复制等待，与event时间重叠，不能相加。
`setup_host_ms`/`read_host_ms`单列准备/读取，event销毁和日志开销仍在
外层残余中。`valid=0`拒绝完整测量，CUDA错误不清除或当零时间处理。
QKV含bias、projection含bias/residual、MLP和merger含其激活与相邻小算子。
严格解析器为`tools.vision_service.forward_timing_analysis`；本阶段边界见
[内部诊断计划](VISION_FORWARD_TIMING_PLAN_2026-11-07.md)。这是诊断选项，
插桩数据不作为关闭计时的性能基线，旧默认和发布包仍保持固定身份。

所有开发 GPU 验证仍应包在 `tools/evalscope/gpu_exclusive.py` 的整阶段
驱动中。以下是 profile 参数示例，不安装后台服务或自动恢复任务：

```bash
./build/q4t serve --host 127.0.0.1 --port 8080 \
  --allow-media --no-mtp --max-seq 1 \
  --max-prefill 8192 --max-len 16384 \
  --max-inflight-requests 8 --max-queued-requests 6

python3 -m tools.examples.vision_query --image /path/document.png \
  --question '请读取表格并汇总各行数值。'

python3 -m tools.examples.vision_query --video /path/clip.mp4 --frames 8 \
  --question '描述画面中物体的起始和结束位置。'

# 显式告诉模型枚举含义，并让约束后端保证语法
python3 -m tools.examples.vision_query --video /path/clip.mp4 --frames 8 \
  --question 'Which direction does the red circle move over the video? Return JSON direction.' \
  --schema-file tools/examples/vision_direction.schema.json --max-tokens 768
```

`--image` 可重复；默认 SSE，`--no-stream` 选择 JSON。客户端先发现能力，
再 preflight，并核对完整回答的 prompt usage；不自动重试、执行工具或
将不完整响应当最终答案。第二个示例是视频诊断入口，不能据此认为
运动方向任务已通过。第三个示例按新的明确语义请求方式有限接受；
`--schema-file`最多64KiB object schema，字段含义应写清。结构化解码只
限制合法token，不自动把schema含义注入原请求，也不保证观察正确。
`--max-tokens`可配置输出预算，默认256，复杂引用回答应显式预留足够
空间；截断报错，不自动加预算或重试502。通用Python/JS及工具最终回答
共享入口见[共享客户端用法和新回归](VISION_CLIENT_RESULT_2026-11-07.md)。同一schema既
进入用户提示也进入response_format；不带schema的默认行为保持。
依赖 OpenCV/FFmpeg 的视频适配器默认 CPU 解码；
本地视频上限 128 MiB、600 秒、7200 解码帧、4096 边长、8.8M 像素，
默认解码截止 30 秒、最高 60 秒、视频采样 payload 12 MiB。
完整 Chat 请求仍受服务 16 MiB body 限制；多图必须计算合计体积。

TLS/独立 API Key 按需启用，后端仍只监听 loopback；客户端支持
`--base-url https://...`、`--ca-file` 和 `--api-key-file`。
实际网段、证书、网关和多租户策略沿用[接入合同](SERVE_CLIENT_GOVERNANCE_2026-11-05.md)，
本阶段只验证了临时本机 TLS 网关，不生成正式部署配置。
视频帧扩展是 Q4T 私有入口；不能把它声明成所有 OpenAI SDK 的原生视频协议。
[OpenAPI](serve-openapi-v1.json) 和[媒体请求 schema](serve-vision-request-v1.schema.json)
分别标出文本及显式媒体入口；现有文本 schema 继续保留。

## 交接与回退

默认程序仍为文本/MTP；视觉必须显式启用。`build/q4t.release.json`
记录本机默认身份、细分接受矩阵及原精确 pair。发布/回退包位于
`.q4t-work/vision-production-20261106/release-v1/`；请保留整个目录及
`previous/dependency-inventory.json` 列出的原包路径。

```bash
# 只读验证包与当前默认的已知身份，无 GPU/模型启动
python3 -B -m tools.vision_service.delivery verify
# 需要回退时：先正常停止自己持有的服务，再执行整对恢复
python3 -B -m tools.vision_service.delivery rollback
```

替换在共享锁下对 ELF 和 sidecar 分别原子 rename，持久化恢复标记。
它不是跨两文件断电原子事务。若中断，使用同一 rollback 入口；未知身份
或其他阶段恢复标记会拒绝覆盖。旧版包含已证明的视觉旋转错误，回退仅
恢复原文本基线，不授予旧版视觉正确性。不得恢复已停止的 vLLM 服务。

用户长稳建议固定已交付 ELF/协议和自有资料，先覆盖图片/工具/SSE/
排队/取消循环，记录完整质量真值、错误码、usage、grammar/队列归零、
RSS/统一 RAM/可用内存、GPU 监控和退出屏障；第 1 小时留检查点再延长。
视频质量与硬解码另立有界任务，不与长稳“运行未崩溃”混为一项。
当前自然图 1508 个视觉 token 的单次响应约 48 秒，表格约 5 秒，
只是受测请求观测值，不是吞吐优化或生产 SLO。大图应先看展开预算，
不要靠超时重试叠加负载。
