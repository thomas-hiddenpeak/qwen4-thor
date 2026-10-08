# MTP 阶段性转正目标（2026-10-08）

## 目标与当前状态

按用户“先有阶段性可转正成果，再继续性能改善”的要求，从研究
提交 `c18f4b5` 启动新 Goal。暂停新增性能候选，以两个独立出口
推进：A 基础正确性修复可合入；B 有限范围 MTP 获得正式支持。
默认启用是另一个决策，不随合入或修改 experimental 标签自动批准。
Goal 当前 active；A 已形成可审查修复 PR，B 尚未通过。既有 NO_GO
和失败原件保留。

保护起点见 `.q4t-work/mtp-release-20261008/starting-identities.json`：
464 项通过，包括两个原 dirty 工作区、main、历史构建及上一候选
174 项来源快照。原研究分支继续保存完整优化与数值研究，不改写
历史提交。模型/reference 只读，不为身份检查读取模型 payload。

## A：基础修复可合入

### 范围与切点

最小完整祖先前缀为 `main 8cd297b → c365b9b`：

- `a3963a3`：首次范围与验收文档。
- `f02e75e`：资源/失败清理、预算接入、生成边界及验证工具。
- `0284b60`：普通 pooled decode RoPE、checkpoint 实际布局与有效性。
- `ec0ae6f`：映射张量尺寸求和，修复 MTP 预算误缩容。
- `c365b9b`：原阶段最后结论；仅文档，不改变运行实现。

该前缀不依赖后续初始化、已知位置、批量 GatherQuant 或计时优化。
它包含跨模式尚未准入的 MTP 实现，默认关闭；合入接受只针对上述
明确修复。RoPE 修复影响普通模式，不将默认关闭解释为主线行为不变。

建立唯一额外的最小合入分支 `codex/mtp-foundation-20261008`，
从 `c365b9b` 开始，独立工作区保存于原项目 `.q4t-work/`。它承担
可审查修复包，原研究分支承担 B，避免将所有研究提交带入主线。
不创建逐实验分支，不自动 merge 或部署。

允许的整理仅限交付报告、证据身份核对及测试编组：现有
`tests/mtp_admission_test.cpp` 保留原源码、断言和已知失败，移入
显式构建/执行的跨模式诊断 target，不登记为基础修复已通过测试。
不通过修改该诊断预期、放宽容差或删除历史失败获得绿色结论。

### 身份与验收

原服务 `7fe34d6` 的 124 项运行源码/构建清单已与 `ec0ae6f` Git
blob 逐项对应，模型库 `473aa421` 未变。03 原直接测试二进制是
封存 `f3c70327`；当前 `build/runtime/q4t_tests` 已是后续测试版，
不得混用。A 必须将合入源码、原受测原件、工具/测试来源及原始
证据绑定为一份可离线核验的清单。

复用候选：22 预算、2 策略、18 runner 合同，CUDA 生命周期，
RoPE/checkpoint 独立反例及修复，普通质量 11、MTP 质量 11、
边界 12、输出上限 off/on 各 6、普通五档 15。普通质量 03→04
按原记录只复用未受 MTP 预算 adapter 影响的执行路径，不能仅凭
文件名或 commit 标签作判断。五档是修复后的描述性记录，不改称
严格性能无回退；必要数值修复不恢复错误计算来维持旧输出。

测试编组整理先静态核对服务目标及链接输入不变，再在新 build
目录只构建所需目标，要求零警告。新服务 SHA 若与原件完全一致，
直接复用 HTTP；若不同，先记录差异并检查原因，未证明执行身份
等价前不得称新二进制已验收。任何必要新 HTTP 的清单与比较规则
须在执行前补冻，不为文档或已证明未变的身份重复推理。

A 出口是可审查的独立修复 PR/提交及验收清单；旧 MTP 跨模式
NO_GO、取消专项的旧整体失败和有限覆盖范围须出现在交付中。
不能把 A 通过写成 B 或本 Goal 整体完成。

## B：有限范围 MTP 正式支持

支持范围先限定为指定 Thor/checkpoint、HTTP `serve`、纯文本、
greedy、S1、k3，正式容量 208896/8192；默认仍关闭。实际入口须
限制未经验证的 MTP 多流/媒体组合；旧 CLI PLE checkpoint 入口
在加载前明确拒绝并返回非零，不在本轮迁移全部入口。

数值产品合同已向用户提供两种选择：优先建议普通 greedy 严格
一致的正确性基线；当前快速 T4 的舍入差异不能靠局部诊断或短
质量题自动获得无损等价结论。A 继续独立进行。具体 B 实现、
数值测试数量与质量门槛在选择明确后另行冻结，冻结前不开始
新的模型测试，也不先改正式支持标签。

两种合同的具体入口、状态所有权、代价和未覆盖项见
[数值准入设计](MTP_NUMERICAL_ADMISSION_DESIGN_2026-10-08.md)。
其中严格逐 token 基线预计慢于普通decode，不冒用快速T4收益。

无论选择哪种合同，B 都必须覆盖：

- 目标 logits/argmax、接受前缀、首次拒绝 correction、next seed。
- main recurrent/PLE、有效 KV/indexer、位置与 history；原
  `ModelSnapshotState` 只有 recurrent，不能冒称完整缓存恢复。
- 生成上限/EOS/上下文尾部、资源失败回退及请求取消后恢复。
  取消应比较同候选、同模式的 fresh control 与恢复，不用普通
  文本差异单独判定 MTP 残留；旧工具失败保留。
- 预先冻结的代表性质量筛查，数值合规与任务质量分别报告。
  旧 11 题只有短答案，不称完整长文本质量；旧质量分数门槛不
  自动恢复为数值修复的一票否决。
- 正式五档性能用于回归与成本披露，不新增提速候选，不使用
  bench、不更换失败基线、不追加有利样本。

支持范围与工具合同可以并行准备；只有具体反例才进行有界定位，
修复后只重验受影响项。B 未达标时保持 Goal 未完成，明确剩余
阻碍，不把一份诊断报告或 experimental 标签变化作为转正成果。

## 阶段交付约定

每个阶段先冻结范围、成组修改，再统一验收；运行时无变化的
证据按身份复用。阶段提交和推送已获授权，PR 可独立审查，main
及部署原件保持。最终分别报告 A 可合入、B 正式支持及默认启用
三个状态，并保留所有首次失败、未运行项和资源/数值覆盖边界。

## B1 支持入口子清单（模型测试前冻结）

此子清单不依赖 strict/fast 数值合同选择，可以与 A 并行准备。
只改 `src/server/server_options.cpp`、`src/main.cpp` 和现有 options
合同测试：MTP 与 `max_seq!=1` 或 `allow_media` 组合在统一
`ValidateServerOptions` 拒绝，解析失败保持原 options 不变。
普通模式的多流/媒体实验入口不变，k3/greedy 及 MTP 默认关闭保持。
非 serve 的 `generate --mtp` 和 `--mtp-k` 在 tokenizer/model 加载
前退出 2，提示使用 HTTP serve；不把旧入口失败留到加载后。
正式支持标签保持 experimental，直到 B 的其他验收全部通过。

本组不改变模型算术或执行 kernel，不在当前阶段单独启动 HTTP。
代码与后续 B 计算变更成组后构建，先执行现有 13 个具名 host
options/chat-contract/MTP-policy 合同，再执行六个进程拒绝检查：
CLI 两项、serve MTP+多流、MTP+媒体、两者同时及未知 `--mtp-k`。
所有进程使用预定不存在的 model-dir，要求明确非零与预期拒绝
原因、没有 tokenizer/model 加载；正常启动由后续质量组覆盖。
默认服务质量和正式五档合并到 B 最终验收，不逐文件反复推理。

## 阶段 A 交付与 B1 验证安排补记

阶段 A 已提交 `92d1d95` 并推送，形成
[PR #2](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/2)。
原始证据独立审计 3201 项通过；新旧执行内容、fatbin 容器、
提取来源分别 292/1418/503 项通过并经独立复核。HTTP 仍引用
旧原件实际运行，不称新构建重新实测。独立修复包已可审查，
main 未合并，MTP 默认关闭，B 与默认启用没有随之获得通过。
PR的[公共host合同](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/37798926553)
已通过，只作本机证据之外的辅助检查。

B 的数值合同仍待明确，root 将无此依赖的 B1 拒绝入口验收
独立安排，避免其长期与尚未选定的计算模式绑定。原 13 组 host
与六项进程拒绝清单不变；使用全新 `build/release-b1-20261008`，
证据写入 `.q4t-work/mtp-release-20261008/b1-local/`。

实际顺序：root 先授权 agent 执行该有界组，后要求构建后稍等
以补充独立验收安排；追加消息到达时 13+6 已运行完成。命令、
范围与原协议在运行前保存，但拆组安排的文字是测试后补记，
不能写成事前已更新。`pretest-hold.json` 与全部时间原样保留，
不重跑取得更整齐记录。仅本机 host/加载前拒绝，无模型/HTTP。
正常 HTTP 和五档仍与 B 最终验收统一完成；后续只在相关身份
改变或具体失败修复时重验受影响项。

## B2a 取消恢复工具修正范围（实现与host测试前冻结）

现有 `decode-recovery` 将普通参考切换为MTP后再比较文本，混入
跨模式差异。保留其历史行为与失败证据；在同一脚本增加
`same-mode-recovery`，禁止使用 `--reference-run` 的普通输出
作为恢复oracle。只修工具，不据此宣布运行时缺陷或取消已通过。

新scope使用候选新服务S1/208896/8192、显式MTP、greedy和固定
seed20260920，从既有quality/performance输入提取45056/1024
fixture，仅作为输入。实际9个生成请求按固定顺序：1K fresh
control；44K prefill显式取消及1K恢复；1K首个非空内容后显式
取消及恢复；同条件TCP RST及恢复；固定3000ms deadline及恢复。
Prefill占槽后等500ms取消，必须在日志证明0<position<45056；
deadline必须先见内容再中断，否则报覆盖失败，不能改时间重采样。
FIN已有host TCP合同，本组不把它冒称真实decode覆盖。

每次恢复的文本/usage/finish、实际模式与MTP步数等于fresh
control；这不是完整内部状态逐位证明。4次中断后均要求健康、
唯一槽回收、aborted+1/success不增；恢复success+1，最终9/4/5。
显式decode/deadline只允许一次error/DONE且无正常finish/usage；
RST保留收到的原始部分响应、真实ID及服务路径，不要求完整终态。
要求8条decode路径（prefill取消尚未进入decode），prefill通过
请求ID、取消响应及日志进度另行绑定。当前实际路径只接受
mtp_multi_b1；未来其他verifier须连同实现更新路径合同。

只扩展 `tools/evalscope/run_request_cancellation.py`，必要的纯
解析/规划helper与 `test_request_cancellation.py`、host CMake
注册。保留旧scope的输入和语义，复用现有HTTP/身份/连接工具，
不另建运行框架。host验证固定为新scope规划/解析反例及原有
response_identity、acceptance_mode两组：覆盖缺control、跨模式、
正常完成误当取消、计数漂移、缺prefill进度、RST部分ID与缺失路径。
不为此重复13+6或模型测试。本轮只完成工具实现和host验收；
上述9请求尚未执行，最终输入/二进制/数值合同明确后另冻执行身份。

## B1 实际结果

B1结果：本机Release构建零警告，13/13具名host、6/6进程拒绝
首次通过；q4t为`3ab3654c`。独立656项只读复核通过，六项
stderr为确切入口诊断而非不存在模型目录错误；无模型/HTTP。
[证据摘要](evidence/mtp-release-20261008/README.md)保留实际顺序，
不将此有限结果扩大为正常启动或B正式支持。

## B2a 工具交付结果

`same-mode-recovery` 已实现并登记host合同；三组首次27/8/18，
共53项通过，无复跑，原始日志与运行前身份已入
[证据索引](evidence/mtp-release-20261008/README.md)。独立静态
审查发现的prefill错误文本/实际ID配对、取消事件顺序、HTTP
拆包、截断、总超时及末次已收字节留证，均在首测前修正。
旧full/decode-recovery语义及历史失败保持，真实9请求尚未执行。

基础修复PR当前头为`5aacc9f`，较92d1d95仅同步README支持范围，
运行来源/构建身份保持，公共host检查通过。B1、B2a均有独立
阶段证据，整个Goal仍active；下一关键门槛是B数值合同的选择
与实现，其后统一运行受影响的状态/质量/五档，不再新增性能候选。
