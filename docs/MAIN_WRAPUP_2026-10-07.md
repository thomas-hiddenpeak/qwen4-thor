# Offload 分支收尾与主线交接（2026-10-07）

本轮从真实 `main` 的 `8ea85b1b0cdf0f026247bf185bddbd536f916c82`
建立独立分支 `codex/main-wrapup-20261007`，只整合以下三个包。
原开发目录仍在 `codex/moe-residency-20260930`，其中七项未提交内容
与原 `build/q4t` 单独保留。收尾不以实验分支整体合并来改变主线运行方式。

## 有界交付与验收范围

| 包 | 来源 | 主线交付 |
|---|---|---|
| 研究工具与结论 | `8602015`、`15feb57`、`8ea3d32`、`a2fdcfd`、`0df355a` | 路由/缓存回放、供给上界、mirror 保留分析、物理 RAM 证据工具和真实响应 ID 合同；历史 runtime 依赖显式指定 |
| 公共 I/O 修复 | 精选 `2b0100c` | 权重读取期间持有 shard 所有权、LRU 驱逐后元数据指针仍有效、诊断读取加锁 |
| 公共预算修复 | 精选 `cb379bd` | 不可行预算在模型加载前拒绝、绑定实际分配参数、MTP 条件计费、PLE 与 KV 页取整修正 |

未引入 MoEResidency、ReadRangev、3b staging、offload 缓存策略、GEMM
映射变化或任何 NO_GO 性能候选。研究用分区算法放在 `tools/trace/research/`，
只供离线工具编译，不进入模型 target。旧计划绑定旧工具摘要，不能直接套用
到移植后的工具；新分析需重新绑定当前工具与明确的历史 runtime 源码身份。

统一验收先冻结以下范围，完成成组修改后一次执行。保留首次失败，只有
具体失败修复后重验受影响项，不扩大为下一轮优化：

1. 公共 host CTest（原合同及移植工具合成合同），响应 ID 与 RAM 工具合同。
2. I/O 生命周期/并发 LRU 直接合同，以及 ASan/UBSan 检查；H2D host stub
   只验证调用/生命周期，不宣称真实设备拷贝。预算边界直接合同。
3. 本机 Thor Release 零警告构建，保持 C++23、CUDA 13.3、SM110a。
4. 实际 1% RAM 预算启动拒绝，确认发生在模型加载之前。
5. 同一新二进制真实 evalscope HTTP 固定质量 11 题，MTP 关闭、单流、
   max-prefill=8192、max-len=208896，与冻结 quality_reference.json 对照。

这是必要正确性修复与工具收尾，不申请提速或性能持平结论；本轮不跑
五档性能矩阵，也不从质量通过推导性能通过。MTP/媒体生成、完整模型
数值 oracle、长稳和整体物理 RAM 54 GB 保证均不在本轮验收范围内。
预算是分配估算，不是实际内存峰值或 OOM 的充分保证。
审查发现真实 workspace sizing 之前还需拒绝超出当前实现上限 8192 的
max-prefill；该入口修复与 0/8192/8193/INT_MAX 边界合同同组验收。
MTP 开启时，独立 BF16 draft 权重、lazy scratch/verify checkpoint 和
并发请求 trunk 重叠并未完整计入；本轮只修正条件计费，不能把 MTP
`feasible=true` 当作完整预算闭合。错误的“trunk 仅一份”峰值说明一并更正。

本地审计根：`.q4t-work/main-wrapup-20261007/`；新构建和测试仅写其
`source/build/`。原始开发状态在 `entry.json` 冻结。测试结果在完成后追加。

## 验收结果与交接

运行源码为 `e6b4bc07b522a8543c51026479c7d141d91ad200`；`c811c11`
只修正供给工具测试夹具路径和过程记录，运行时身份未变。新 q4t SHA256：
`0f52e926d28f2153e63b4eb093e596448e88e36dad355fb632cf1c2da029b8bc`。

| 验证 | 结果与范围 |
|---|---|
| 本机构建 | Thor Release，g++ 14.2、CUDA 13.3.33、C++23/SM110a，零警告 |
| 公共 host | 18 个 CTest 组、331 个命名合同完成；首轮 17/18 组通过，唯一失败组修复后 19/19 定向通过，其他 17 组复用 |
| I/O | 6/6，ASan/UBSan 同 6 项通过；设备调用用 host double，真实模型加载由下方 HTTP 覆盖 |
| 预算 | 14/14；四类实际入口拒绝通过（1% 无解预算及 8193/214748365/INT_MAX prefill），均未进入模型加载 |
| HTTP 质量 | 11/11，与冻结参考精确一致，覆盖 1K/4K/8K/44K/200K，usage/stop/真实响应 ID 正确；服务退出 0，端口释放 |
| 独立审计 | I/O、预算、入口边界、源码范围及验证记录交叉复核；原目录七项 diff 和旧二进制摘要不变 |
| 公共 CI | [c811c11 检查通过](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/37568128798)，仅证明实际执行的 host 检查 |

HTTP 实际容量保持 max-seq=1、max-prefill=8192、max-len=208896，MTP/
媒体关闭，没有预算降档。预算报告主 workspace=2,348,023,808 bytes，
state pool=6,546,454,540 bytes；这些是模型项估算，不是整机 RAM 测量。
本轮没有五档性能接受、默认部署或 MTP/媒体生成验收。

首次失败均保留：供给分析测试曾使用旧仓库父目录落点；只改夹具，
生产 artifact guard 未变。入口脚本首次错误匹配报错文案，实际程序
已正确拒绝；更正文案后复验通过。该脚本原 `phase=model_load` 断言
与实际日志格式不匹配，最终另按真实 `[q4t][startup]` 日志及源码返回
位置审计，未用弱断言充当证明。冻结计划中公共组数 19 是静态计数笔误，
实测注册为 18；未删除测试。原计划、失败和更正记录均不覆盖。

精简证据在 [evidence/main-wrapup-20261007/](evidence/main-wrapup-20261007/README.md)，
包含来源、源码与二进制身份、首次失败、定向重验、HTTP 摘要及保护检查。
原始数据库/请求/服务日志仍在本机 `source/build/http-quality-01/`，
完整路径与摘要见 quality-audit.json。

后续主线工作区：
`.q4t-work/main-wrapup-20261007/source/`，交接后检出 `main`。
已验收二进制位于该工作区的 `build/runtime/q4t`；未替换原开发目录的
`build/q4t`，也未写入旧部署元数据。原目录仍保留研究分支和七项未提交
内容，后续主线任务应在这个干净工作区开展。研究分支保留作查证入口。

## 研究分支的保留价值与停止点

所有 offload 后续分支均汇入同一研究历史，最新封存 tip 为
`7672b591644add0bbc05b8782da3127c80edc04d`；远端分支与原始证据保留，
后续本地分支引用整理见文末。
这些分支有研究价值，但不等于已有待默认启用的性能提升。

| 研究线 | 保留结论与入口 |
|---|---|
| 分区及按请求策略 | 完整性能筛查 NO_GO；[分区报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_PARTITION_RUNTIME_2026-10-03.md)、[请求策略](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_REQUEST_POLICY_2026-10-04.md) |
| 诊断与历史影响 | 前驱/后继存在可观测差异，单独因果未闭合；[诊断](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_DIAGNOSTICS_2026-10-04.md)、[受控历史](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_CAUSALITY_2026-10-05.md)、[机制](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_MECHANISM_2026-10-06.md) |
| Decode 日志 | 去掉目标日志仍完整性能 NO_GO；[报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_DECODE_LOG_2026-10-05.md) |
| GPU 源码一致回放 | 固定入口与顺序下复现基线；expert-ID 平票候选增加工作量，未准入 runtime；[报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_GPU_REPLAY_2026-10-06.md) |
| Decode 供给与 mirror 保留 | 同 plan 竞争存在，但不能解释全部供给差；保留分析可约束下一次假设；[供给报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_DECODE_SUPPLY_2026-10-06.md)、[保留报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_MIRROR_RETENTION_2026-10-06.md) |
| GPU-covered mirror 回收 | 17 个服务、131 个 HTTP 的固定范围完成，性能 NO_GO；不继续追加策略；[最终报告](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7672b591644add0bbc05b8782da3127c80edc04d/docs/OFFLOAD_MIRROR_RECYCLE_2026-10-06.md) |

这些结论只适用于各自报告中的源码、二进制、输入和规则。移植工具的
合成测试不重复证明历史实测结果，也不把离线加载次数当作 HTTP 吞吐。
完整 offload 能力今后若进入主线，需要单独范围、依赖审查和新的验收；
本轮结束后主线可直接推进其他任务，不依赖继续探索 offload。

## 本地分支整理（2026-10-07）

按用户减少重复分支的要求，本地分支从 18 个收敛为以下 4 个：

| 保留分支 | 用途 |
|---|---|
| `main` | 后续正式开发入口；整理前为 `d045cef` |
| `codex/offload-mirror-recycle-20261006` | `7672b59`，完整已提交研究历史的统一入口，仍非主线正式 offload 能力 |
| `codex/moe-residency-20260930` | `f15190f`，原目录 7 项未提交修改，暂时保留 |
| `codex/moe-residency-path-c-20261001` | `887a3fe`，wt-c1 另有 2 项未提交修改，暂时保留 |

祖先关系逐项核对：全部 15 个研究分支的提交均被 `7672b59` 包含；
main-wrapup 与 storage-ready 两个分支均被 main 包含。path-c 虽领先
同名远端 3 个提交，但三者均已在远端研究入口中保存；其未提交修改
涉及 model.h 和 chat_generation.cpp，不能因此丢弃。

移除 14 个重复本地分支前保存名称、完整 SHA、远端引用、工作区状态
与两处未提交补丁。10 个干净旧工作区在同一 SHA 切换为 detached HEAD，
随后使用 `git branch -d` 删除本地引用。所有 15 个工作区目录、源码、
构建、venv 与原始证据保持原路径；没有删除或移动工作区。

本地审计目录：`.q4t-work/branch-consolidation-20261007/`，其中
`plan.json`、`refs-before.txt` 保存恢复映射，`actions.jsonl` 记录操作，
`result.json` 记录核验。所有旧工作区 HEAD 与 diff 在整理前后相同，
根目录 7 项和 wt-c1 2 项修改、两个 q4t 摘要不变。远端分支引用在
整理操作前后完全相同；后续仅 main 推进本次文档记录。

这是入口整理，不释放显著磁盘空间，也不新增性能或质量结论。
运行时代码没有变化，不重复构建与推理。同一阶段后续只维护一个活动
工作分支，用提交划分子任务；本轮没有为整理另建分支。长期入口以
main 和一个完整研究归档为主，两个暂留分支待未提交内容明确归宿后整理。
