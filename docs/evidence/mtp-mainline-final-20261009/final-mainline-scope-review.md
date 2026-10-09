# 最终主线交付范围独立审查

2026-10-09；只读审查，未修改源码/测试、建分支、构建或运行模型。
比较身份为 main `8cd297b8af62701b00831ac44704e6b0691753d9`、
研究分支 `90d5f539a77b6ab2d6c5bc5fbaa2cadfced778d1`，
基础包 `5aacc9f225056b47fca766dcf9d49a170e60978f`。
后续 completion / terminal 候选不计作这三个提交已有内容。

**建议交付正式的 opt-in sequential MTP，同时保持普通生成默认、
T4 显式实验。** 阶段交付不以现在提速、默认打开 MTP、所有硬件/
并发都获支持为前提；但必须有真实闭合的生命周期、终止消费合同、
常规测试入口、当前候选成本及可审查的主线集成结果。改标签或将
PR 设为 ready 不能替代这些工作。约 10% 的旧 strict 成本不能成为
保留错误计算的理由，也不能直接当作新候选实测。

## 增量大小及真正的依赖

main 到研究分支共 389 个变更文件，新增 62,604 / 删除 458 行。
其中运行源码和头文件为 29 文件 +4,128/-386；测试 +9,395/-23、
工具 +6,989/-41；284 个证据文件占 +38,112 行。20K+ 的源码/
测试/工具增量确实增加审查负担，但不是 20K 行都在生成主路径中。
PR #3 包含历史并不自动阻止合入，也不能据此称它已经是最小产品差异。

| 内容 | 生产依赖及实际边界 | 本次建议 |
|---|---|---|
| sequential driver | `src/mtp/mtp_sequential.cpp:115–149` 每行调用普通 `ModelDecodeBatchMulti(B=1)` 和同一 target argmax；只接受相等的非 stop 草稿；已消费前缀与待输出 correction 分开 | 正式路径，必须保留 |
| 共享草稿/初始化 | driver 的 draft/extend 调 `MtpForward`；HTTP 初始化在 `chat_generation.cpp:696–705` 实际采用 last-row，并在长 S1 初始化采用 skip-tail；共享 workspace/scratch、BF16 MoE、KV 均在运行 | 不能把所有历史优化当可直接删除的独立研究代码；本组共享位置修复须验收 |
| 普通 target 与基础修复 | pooled RoPE、checkpoint 布局、MTP 资源预算/清理、输出尾部与请求生命周期为 PR #2 的可独立基础包；PR #3 已含其大部分代码祖先 | 独立审基础包，避免两份重复实现 |
| HTTP 接线 | `server_options.cpp:22–29,53–62`、`chat_scheduler.cpp:367–385`、`chat_generation.cpp:645–705,939–963` 共同决定实际 verifier、容量/模式限制、前置资源回退和 B1 尾部 | 正式路径，产品语义必须与日志、工具一致 |
| T4 verify/checkpoint | sequential 不调用 `ModelVerifyMulti` 或 restore，初始化仅非 sequential 才 reserve verify checkpoints（`chat_generation.cpp:709–710`）；二者仍被显式 T4 引用 | 可保留实验，但已确认的完成/stop/tail 错误应修好，不能以实验标签掩盖可达状态错误 |
| 研究计时 | `CMakeLists.txt:157–163,185–188` 将 trace/init timing 编入生产；普通 attention/linear/MoE 中保留 hook。无 active scope 时 span 不读时钟（`mtp_cycle_timing.cpp:128–140`），verify scope 为线程局部；服务仅按显式环境启用 collector | 可保留，不能声称完全无依赖/零开销；最终成本测量关闭这些环境 |
| T4 batch gather / capture | `moe_gemm.cu:380–398` 默认关闭且仅支持固定 T4 形状；生产入口 `:898–906` 永远传 capture=null。测试入口 `:909–917` 才能传捕获/注入对象 | 可保留 optional；无需本轮拆 TU 或新增提速候选。测试接口实际编进库需在审查中明说 |
| 研究夹具与分析工具 | cross-mode 已知失败和五类 raw/env 夹具在 `CMakeLists.txt:328–344` 独立 EXCLUDE_FROM_ALL；真实 admission、HC 与故障 server 也为显式 target | 保留原断言/历史；不属于正常服务的文件依赖 |
| 正式控制测试 | `CMakeLists.txt:355–364` 默认构建并注册 sequential 生产 TU 控制；scheduler 三个合同另有 CTest；公共 host 入口运行完整 host 注册表 | 已消除原“研究测试默认跑、正式控制反而缺入口”的阻碍 |

研究环境需要分开解释。当前正式匹配检查排除 FP8/GDN 算术覆盖、
batch gather 和 MoE streams 覆盖；未知 checkpoint 不被日志认证。
sequential 对 cycle / verify-MoE timing 直接拒绝，init timing 仍可显式
启用。plain 默认不加载 MTP；裸 `--mtp` 现在选择 sequential；
generate 的 MTP 参数在加载前拒绝。以上为代码行为，不仅是文档。
MTP 多序列和媒体被入口拒绝，不能因底层保留 pooled API 就称已支持。

## 实质剩余清单

1. **完成并验证共享位置/完成边界修复。** 当前 M1 没有解决这个
   实际缺陷。完成候选的独立审查已写 `t4-completion-independent-review.md`；
   按冻结的 18 例真实 CUDA、9 个 T4 故障/恢复和 1 个 strict HTTP
   验证。每次实际失败保留，只有修复后重验受影响项。
2. **闭合仍然对外可达的 T4 终止/消费错误。** stop 预测不能被多消费，
   最后允许输出不能借整批 accepted_count 推进；terminal 不应产生
   无用 extend；不足完整步的输出/上下文必须走正确尾部。按另冻的
   真实生产控制与有限 HTTP 合同验证。这个要求不等于要求本次证明
   T4 全链路数值等价或实现 T4 提速。若最终不修该路径，替代方案必须
   是真实限制生产入口，不能仅将文档改成 experimental。
3. **成组冻结最终源码/构建身份并做一次适用检查。** 只复跑共享依赖
   实际受影响的 build/host/control/tool 项；沿用已证明未变的严格
   target、取消/资源回退、长状态等证据，并逐项列复用原因。普通
   CTest 的模型/媒体 fixture 检查不能冒称全绿；研究已知失败不能被
   改断言、改 skip 规则或挪 target 解释为修复。
4. **当前候选五档成本一次验收。** 见下一节。共享运行时位置代码改变后，
   旧 strict 五档保持原身份；不能只凭“改动应无算术变化”将它们重新
   标为当前 binary 成本。
5. **形成可维护的合入清单和实际主线交付。** 明列正式入口及可复现测试
   命令/精确名称、支持参数、当前成本、实验路径和退出/回滚方式。
   `tools/release/README.md:3–5,59–62` 当前仍只描述 text-v1/MTP-off
   发布门禁，应追加或链接独立的 MTP 正式交付门禁，保留旧范围；
   无需新建庞大通用验收框架。最后由独立审查确认没有遗留必做项，
   再按总 Goal 的主线纳入目标处理实际合入/交付记录。

主线集成最小做法：沿用现有两个 PR 与同一研究分支，优先处理 PR #2，
再将 PR #3 的剩余差异对新的 main 核对；用追加提交解决交接与合并
冲突。foundation 的两个整理提交不是研究分支祖先，不能假定合入
完全无冲突；尤其 STATUS/README/CMake 必须保留最终实际语义，
不能整文件选择旧一侧。不要为了行数重写研究历史、新增多条内容
相同分支，或删除已封存失败。证据和工具可在审查清单中分组，生产
共享代码仍须逐项可审。仅研究目录重排不值得触发另一轮模型测试。

## 最后一次 30 请求五档：建议执行

建议在 completion 和 terminal 两组实际修复全部完成、最终编译与
直接合同通过后，统一运行最终生产 binary 的
**plain / sequential × 1K、4K、8K、44K、200K × 每档三次 = 30 请求**。
不要每合并一个文件重跑五档，也不插入 T4 提速候选。

使用原冻结的固定输入字节/seed/greedy/max256、正式 S1/k3、
208896/8192、同 checkpoint/精度、现有 evalscope HTTP transport，
保存实际容量与 verifier/path、原 DB/响应/usage/finish、环境和
binary SHA。请求顺序在运行前冻结；首请求与后续请求单列，不发
额外探针、暖场、挑选结果或事后改阈值。每档三次是成本观察的最低
重复，不证明细小差异的统计显著性。

这 30 条提供两件事：最终 binary 在五档的 HTTP/输出/实际路径回归；
同一候选 plain 对 sequential 的当前 TTFT、decode 和整请求代价。
它们不自动证明相对历史 main 的因果零回退，也不代替完整任务质量、
取消或状态证据。若需要声称对某历史基线性能保持，必须使用事前
绑定且口径相同的重复波动规则；不能从新的三次结果反推容忍阈值。

必要正确性修复和正式 opt-in 功能的成本须如实接受或说明；不能以
错误旧路径更快否决修复。普通路径的意外损失、实际 fallback、
输入/输出不一致或新的状态错误须调查并冻结针对性修复。sequential
仍慢本身不要求再做一个优化候选，不能为了 Goal 看起来成功继续
寻找有利样本。性能继续改善是本次正式交付之后的独立目标。

## 完成判定

技术条件必须真实闭合：正确入口、生命周期/终止合同、可维护测试、
当前成本与可审查的最终差异。PR ready 只是流程状态；有限样本的
文字包装不是新的产品能力。阶段功能可以是“支持范围明确、可显式
开启的 sequential MTP”，无需把默认服务改为 MTP，也无需本阶段
将 T4 宣传为正式加速方案。若总 Goal 要求纳入 main，最终还须记录
实际主线提交与依赖落地；若尚待合并决策，准确报告“已具备经审查
的合入条件、尚未合入”，不要再次把未完成的主线动作说成已完成。

