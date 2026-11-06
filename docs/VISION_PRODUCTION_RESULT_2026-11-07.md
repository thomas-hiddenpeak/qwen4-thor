# 视觉服务 v1：限定接受与生产准备

2026-11-07，依据[冻结计划](VISION_PRODUCTION_PLAN_2026-11-06.md)。
结论为 `PASS_SCOPED_VISION_SERVICE_V1_WITH_TASK_LIMITS`：图片业务、
视觉数值/输入、输出协议、实际工具往返和配额治理已有限接受；视频
方向任务、硬件解码和长稳未接受。不是“全视觉已完善”或正式部署结论。
接口、组合拒绝和操作见[运维手册](VISION_SERVICE_V1.md)。

## 关键修复与独立证据

旧视觉 RoPE 将 72 维误拆为两个独立 36 维旋转；旧 NumPy 参考复制了
同一错误。官方 Transformers 5.16.1 CPU 的 27 层完整塔使用固定权重、
相同 BF16 输入，证明旧 CUDA 的 rel_L2 为 .401485/.420245，不能再
引用旧的“数值通过”。修正完整 head 跨 36 维配对及 merger 的 erf GELU
后，四个 image/video、正方形/矩形夹具结果如下，原 .05/.05 门限不变：

| 夹具 | 元素数 | max_abs | rel_L2 |
|---|---:|---:|---:|
| small image | 10240 | .007106 | .022225 |
| small video | 20480 | .012458 | .029820 |
| rectangular image | 61440 | .012091 | .029298 |
| rectangular video | 76800 | .008411 | .024169 |

原始 BF16 数组、官方 HF FP32 输出、来源摘要和负对照均保留。
另修视频每时间组时间戳/视觉边界、MRoPE 最大坐标及拒绝坏网格、
Python nearest-even resize、共享媒体展开/preflight、展开后 TPM，
加载前固定 checkpoint 配置校验和可取消视觉锁等待。默认文本/MTP
保护的 JSON/SSE、strict JSON、真实项目资料工具两跳通过。

CPU 证据包括 322 host、80 ASan/UBSan 规划、101 固定参考几何/MRoPE、
6 实际 tokenizer/模板差分、4 输入适配、5 查询客户端用例；定向驱动、
协议生成与 114 来源锚点/38字段/10路由审查通过。已通过且身份未变的
检查复用。JPEG 首轮严格像素对比为 7/8，原失败保留：本夹具 stb 与
Pillow 最大差 1 uint8，后续 JPEG 与自身解码 PNG 流程完全一致，实际
PNG/JPEG 表格都答对；不外推任意 JPEG 位等或误差上限。

## 真实业务与失败边界

生成自然图、可重复文档/图表和 8 帧红圆移动视频均有独立真值。
表格 PNG/JPEG 正确读为 Vision=12、Text=24、Tools=8；图表最大 B，
多图结果及颜色/空间任务正确。视觉工具真实读取本项目 `docs/STATUS.md`
并回填引用，不用伪造工具结果。输入拒绝、显式取消与后续恢复通过。

phase-04 整轮原状态仍为 FAILED：正向、反向、奇数视频和硬编码后 CPU
解码视频的 strict 方向枚举都反转。后续 4 个限定诊断得到首帧 Left、
末帧 Right、普通视频描述 left→right、两端图片描述正确；它们不能替换
原失败或证明原因仅在结构化约束，prompt 也不同。`json_object` 返回
正确 red 但多 confidence 被原 exact oracle 误拒，独立复核遵循原接口
允许附加键的合同；原结果不改。schema 保证与任务准确性分别记录。

TLS/独立 Key、展开 TPM、RPM、客户端轮转及取消的 13 次 Chat 有
6 正确完整回答、2 取消、5 拒绝。原驱动混淆槽准入逻辑计费与生成统计
导致 FAILED；独立 CPU 核账按既有合同得到 2928 = 2508 已完成生成
tokens + 420 已准入取消 prompt，排队退款 447、最终预留 0。不是
服务器计费补丁，限定接受来自原 wire/ledger 重建，不追加模型请求。

一次启动前容量配置失败（alpha6+beta6 > 全局8，0 HTTP）还暴露驱动
遇非零退出时跳过自身 reap/GPU 屏障。共享 guard 最后观察空并释放锁，
但该次 custody **不接受**。已修驱动记录非零 waitable 后继续清理，
6 CPU 驱动用例通过；后续阶段自身退出屏障完整。未把旧失败追改通过。

Jetson NVENC H.264 8 帧编码成功；硬解码 `nvv4l2decoder` 报 buffer
pool 分配/激活失败，未接受。最初 ffprobe 元数据失败也保留，缺失的
子进程 stderr 不补造原因。生产示例使用有界 CPU 解码，显式 Jetson
实验入口保留且不静默降级。长稳、真实 LAN、全物理 RAM、托管/持久
任务迁移、全 SDK 和新的性能优化均没有接受结论。

## 冻结预算与证据

累计 6 次模型启动尝试（5 次完整加载、1 次加载前拒绝）、70 次 Chat
尝试、67 次控制请求、5 个顶层原生 worker，GPU 租约共 991.534 秒。
阶段完整持同一共享锁，无干预其他服务。最后 phase-07 为 COMPLETED，
自有进程、端口、GPU、租约已释放；不再追加 GPU/模型/原生测试。

可移交的摘要/原件哈希见[evidence](evidence/vision-service-v1-20261107/result.json)。
4项发布文件事务故障/回退检查通过；不运行模型或GPU。
本机证据根 `.q4t-work/vision-production-20261106/`：

- `audit-03/result.json` 独立重建业务/数值/协议结果，`originals.json`
  绑定 943 原件，`binding.json` 绑定报告、验证器、CAS。
- `phase-01` 至 `phase-07`（含 `phase-05b`）原计划、wire、进程/租约
  和失败全部保留；每阶段冻结源码由 `frozen-inputs/<sha256>` 保存。
- `cpu/` 留存前后差分和全部失败；`audit-01.log` 的历史 CAS 缺项及
  `audit-02.log` 的 custody 发现均保留，不覆盖初次输出。
- `release-v1/` 封存候选、精确父 pair、声明/schema、运行来源、结果
  和额外 CPU 清理证据；`delivery-v1/` 记录文件安装及 CLI 身份检查。

候选 ELF `ea1b474d7d69808696e697346a60ee09bb9ecaca9bfb99925b78dcdbda7a4550`，
协议 `91b619c90ce8f488436443e904973f37ffb25bc6b7e53803526df2f909bd063a`。
运行修复来源 `bdd7ce8c`，后续工具和文档不改变已验 ELF。父 ELF
`aa528a68` / sidecar `46aa73bd` 精确保留，旧托管/任务/Key/day 包
及其原路径不自动迁移。

发布前先核对已验字节和原件，再在共享锁下替换适用默认 pair；安装
检查限于 CLI/哈希/声明，HTTP 健康复用同 ELF 的 phase-07，**没有新的
安装后模型启动**。当前预算已用尽，不以重复启动包装为新的完整验收。
机器声明全局 `media_acceptance: false` 保守保留，另列图片已验、视频
数值已验/任务未验矩阵。实际交付身份和主线状态以本文件追加交付记录
及 STATUS 为准。

## 本机交付记录

2026-11-07 已交付默认 ELF `ea1b474d` / sidecar `3eb46385` / 协议
`91b619c9`。[安装记录](evidence/vision-service-v1-20261107/installation.json)
为 PASS_INSTALLED_PAIR；241份运行来源与943原件复核，整包封印通过。
本次仅版本CLI与身份/声明核对，新增模型/HTTP/GPU执行为0，HTTP保护
仍引用同一ELF的phase-07。旧aa528a68/46aa73bd整对及原依赖不变。
[收尾观察](evidence/vision-service-v1-20261107/closeout.json)确认12个自有
waitable进程身份已退出、18146/18466无监听、GPU观察为空、共享锁可用，
恢复标记清除；无常驻服务。观察不能证明采样间隙没有其他工作负载。
包封印与资料快照在安装前形成，保留当时“准备交付”文字；本段和独立
安装收据补充实际结果，不改写已封存的文件。
