# 当前状态

更新：2026-10-19。代码是实现事实来源，验收以[EVALUATION.md](EVALUATION.md)为准。

## 当前交付

**真实输入诊断host合同已完成首组验证（2026-10-19）。** 新增严格
layer-boundary文件读取、逻辑位置/完整前缀对齐、按expert对齐的分项
差异报告，以及显式捕获上下文。8项报告测试＋1项上下文测试通过，
C++零警告；尚未接入真实模型捕获/重放，GPU与HTTP新增0。
见[host证据](evidence/moe-replay-real-20261019/host-result.json)及
[数据合同](../tools/verify/moe_replay_real/README.md)。

**真实文本MoE诊断目标已启动（2026-10-19）。** 用户授权继续并设置
目标。已核对旧observer的层/位置推断局限及真实MoE调用边界，优先采用
独立链接包装和显式teacher-forced上下文，补真实层输入与分项报告。
准备期GPU/HTTP新增预算0，完整运行清单实施后、采样前冻结。
见[阶段范围与完成条件](MOE_REPLAY_REAL_INPUT_PLAN_2026-10-19.md)。

**A1复现驱动补强（2026-10-19）。** 两进程改为使用冻结夹具副本，
运行前后校验输入/源码快照；准备阶段失败及KeyboardInterrupt均保存
明确终态。5项纯host回归通过，未修改CUDA/路由/生产路径，GPU与HTTP
新增0，原数值证据按未变依赖复用。
见[补强证据](evidence/moe-replay-driver-hardening-20261019/result.json)。

**主干 MoE 路由重放 A1 已通过诊断合同（2026-10-19）。** 独立CLI/API
支持固定有序IDs与当前分数；host 2/2、5组真实层同形状逐位重放、
5组生产正常路径对照、CPU参考/边界、4项GPU拒绝和错误后缓冲复用通过。
独立文件回放及显式跨binary补验通过；T1/T4本固定夹具无差异，不扩展
为一般batch不变或整模型质量结论。生产MoE全部可加载段/指令保持，
HTTP复用B1b质量与五档证据，新增HTTP 0；默认B1a未替换。
见[最终报告、身份与边界](MOE_REPLAY_A1_2026-10-19.md)及
[复现入口](../tools/verify/moe_replay/README.md)。


**B1b已独立工程接受并合入main（2026-10-19）。**
PR #7合并提交a2f5af8；最终审核头88eb43b，两项host检查通过，合并树一致。
状态ENGINEERING_ACCEPTED_WITH_TIMING_UNCERTAINTY；统计性能仍为
INSUFFICIENT_EVIDENCE，不宣称提速、持平或非劣。用户已单独认可本项
普通decode最大观察下降0.0789%、最大TTFT增加88.38ms、真实退化上界
未约束及86.48秒工具故障锁间隔风险，不继承B1a决定。
复用零警告构建、82 HTTP、候选37/37、父8/8及12步状态逐位差分；
新增采样0。默认build/q4t仍为B1a，未部署B1b，不启动Megatron。
见[本项决定、全部观察与回退](MTP_GATHER_B1B_2026-10-19.md)及
[PR #7](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/7)。

以下历史入口中的旧生产binary与分支描述不覆盖上述及B1a部署记录。


**B1a已工程接受并合入main（2026-10-18）。** 用户在最终审查披露风险
后要求并入主线，按ENGINEERING_ACCEPTED_WITH_TIMING_UNCERTAINTY接受，
有限工程接受规则纳入EVALUATION。复用原零警告构建、计算/状态合同、
HTTP质量及两模式五档证据；不追认统计持平。no-MTP 200K decode观察
下降0.2526%及未约束的真实退化风险保持披露。新增采样0，B1b不扩展，
build/q4t已部署已验证B（44abd8b1），A保留可回退。当前无运行服务，
只更新默认启动入口与身份元数据；没有新增上线健康证明。
见[部署记录](MTP_POSITION_B1A_DEPLOY_2026-10-18.md)。
[PR #6](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/6)合并提交c3f3c45，
工作分支已核对包含后清理，原证据路径保留。
见[主线交付记录](MTP_POSITION_B1A_MERGE_2026-10-18.md)。

以下审查条目保留当时状态，待认可标记已由上述决定替代。

**B1a零新增采样最终审查（2026-10-18）。** 建议有限工程接受，状态
RECOMMEND_ACCEPT_PENDING_APPROVAL；现行EVALUATION未改，原统计性能
验收未通过，未合入/部署，生产A保持。三份清单1136条记录及20项计时
复核通过；没有发现本次改动的关键合同遗漏或确认可归因退化，但不能
排除小幅真实退化。撤回300请求方案及未经实机验证的0.2%能力门禁，
保留历史；新增预算0、不扩展B1b。下一步仅请用户认可具体风险和规则
修订，不自动采样。见[最终审查](MTP_POSITION_B1A_FINAL_REVIEW_2026-10-18.md)、
[待认可草案](EVALUATION_SIMPLIFICATION_DRAFT_2026-10-18.md)。

以下为阶段历史；其中旧下一步预算/建议不覆盖上述当前决定。

**B1a同期60请求补验完成（2026-10-18）。** 按用户“缺什么补什么”
执行冻结A-off/B-off/B-on/A-on四服务，60/60输出检查通过，单lease约
58分19秒，原始20个DB核验，无重试/追加采样。补齐精确父on与两模式
同期对照；MTP decode五档观察提升0.2651%～1.1026%，no-MTP 200K
观察下降0.2526%，不能用前者抵消后者或推定因果。剩余缺口明确为
跨启动差值区间和独立分辨带；不再重复描述采样、不自动转校准。
B1a未合入，生产A保持，不扩展B1b。见[补验结果](MTP_POSITION_B1A_AB60_RESULT_2026-10-18.md)、
[原冻结卡](MTP_POSITION_B1A_AB60_PLAN_2026-10-18.md)。

**B1a增量证据离线审查（2026-10-18）。** 新no-MTP基线补齐精确父版本
off质量/五档记录及一个A/A观察；未补齐同期候选对照、MTP-on测量能力
或20项分辨带/差值区间。23项关键证据SHA与旧manifest一致，旧候选对
新A0/A1的部分差值会变号，不能据此判噪声或通过。建议0请求收口；
60请求仅列作完整同期描述对照的条件成本，不是执行卡或GO保证。B1a
继续暂停、不扩展B1b。见[增量审查](MTP_POSITION_B1A_AFTER_BASELINE_2026-10-18.md)。

**no-MTP描述基线采集完成（2026-10-18）。** 当前生产binary8685d8bf
质量11/11、A0/A1各五档15/15通过；严格41请求/3服务，单lease约35分钟，
正常退出，无追加采样或校准。TTFT两次启动观察差异−0.0095%～+0.4929%，
decode下降量−0.0265%～+0.3030%；只作描述，不建立分辨带或持平结论。
按需校准预算0，B1a继续暂停、不扩展B1b。见[实测结果与证据](NO_MTP_BASELINE_RESULT_2026-10-18.md)、
[41请求冻结方案](NO_MTP_BASELINE_BOUNDED_2026-10-18.md)。

**B1a方法冻结（2026-10-18）。** 60请求每模式仅两次服务启动、一个
A/A差值，不能建立可信分辨带；150请求设计也缺少误差/漂移模型依据。
撤回先60再90的可执行建议，方法记METHOD_INSUFFICIENT，采样前停止，
当前新增预算0；候选性能仍证据不足，不是NO_GO。不扩展B1b。
见[统计方法与停止判定](MTP_POSITION_B1A_METHOD_2026-10-18.md)。

**B1a离线复审（2026-10-18）。** 未发现位置下传实现缺陷，原计算/HTTP
结论按覆盖范围保留；355份原始文件与历史18项关键证据核验、计时复算
一致。现有数据不能补齐同期父版本对照、A/A分辨带与差值不确定区间。
此前提出最多150请求的条件补验（60 A/A门禁＋90交叉对照），现已由
上述方法冻结撤回；本轮新增采样0，不扩展B1b。见[复审与提案](MTP_POSITION_B1A_REVIEW_2026-10-18.md)。

**B1a有界交付完成（2026-10-17），性能证据不足。** 精确位置仅下传至
multi草稿循环/非终止extend，构建零警告；HTTP两组质量各11/11、五档
各3次输出检查、9项直接合同及尾部/取消均通过。共60请求、无补采样，
GPU guard正常结束。各档可信分辨带未建立，不宣称持平/提速或真实回退。
候选44abd8b1保留于codex/mtp-position-b1a，不合入main；生产build/q4t
仍为8685d8bf。[实现、判定与证据](MTP_POSITION_B1A_2026-10-17.md)。

**项目描述核对完成（2026-10-17）。** README/简介、CLI展示、架构与
索引已按代码修正，详见[核对记录](DESCRIPTION_AUDIT_2026-10-17.md)。
仅CLI字符串/注释及文档变化，推理控制与数值逻辑未改。q4t零警告构建，
版本/帮助已检查；无模型测试或采样，不宣称新性能验收。

[PR #5](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/5)已于
2026-10-17 09:06:07 UTC按squash合入main，合并提交f3a975c。
最终22文件差异已审查；保留工具/测试身份、生产源码/binary及文档链接
核对通过，最终PR头2046b6a两条既有host-contracts检查均成功。
合并树与审核PR头完全一致；本轮未重复运行任何测试。
见[交付与证据](MAIN_CONVERGENCE_2026-10-17.md)。

此前收敛时工作区切到main，原工作分支本地/远端引用已删除。最终PR头由
archive/main-convergence-delivery-20261017标签保护并从远端独立取回验证；
完整研究与两个旧研究分支仍按[归档索引](ARCHIVED_RESEARCH.md)恢复。

## 已接受生产基线

B1b源码按PR #7合入；此前main为a893dc8，包含B1a合并c3f3c45。
默认build/q4t及release元数据为已接受B1a binary
44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03，
对应真实构建缓存与回退见[B1a部署](MTP_POSITION_B1A_DEPLOY_2026-10-18.md)。
8685d8bf与更早be74946d是前代基线，不是当前默认入口。
默认MTP k=3，--no-mtp回退普通decode；PLE上传修复与N=4等合同保留。
[数值收口](MTP_NUMERICAL_CLOSURE_2026-10-16.md)的未变依赖证据继续复用，
B1b候选独立保存在.q4t-work/b1b-20261019/build/q4t，未替换默认入口。

HC up、T4 attention和GDN Record/Fold候选均未接受；HC TTFT根因未明，
原因果目标blocked未完成，不自动恢复诊断或把差异称为噪声。微小变化
判定仍受测量分辨能力限制，各档分辨带未建立，不任意填百分比。
旧参考研究计划仅作归档素材，不是自动执行队列。

## 分支与证据

根工作区为main。B1b本地/远端分支codex/mtp-gather-b1b已清理，旧tip
88eb43ba2ea9b22cf1e240e900eed42ac7c256c9完整包含于main合并提交
a2f5af8d1b9e979baac8c5fb4e13dcdbf7819f9a；删除前核对远端tip、无活动PR、
无独有提交/未提交工作，远端删除带预期tip保护。原证据路径保留。
B1a分支已清理。既有归档研究与其他worktree未改动。

历史状态保存在main及研究tag，不作为当前任务安排：
[生产基线历史状态](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/28a3783350b0c4c3d73fd3d483b5eebe881d2bdb/docs/STATUS.md)。
