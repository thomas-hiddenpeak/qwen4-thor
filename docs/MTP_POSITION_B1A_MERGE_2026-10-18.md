# B1a主线交付

2026-10-18。ENGINEERING_ACCEPTED_WITH_TIMING_UNCERTAINTY。
[PR #6](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/6)已于
2026-10-18 06:41:26 UTC合入main，合并提交
c3f3c456bb53c715763c56940703868c1b3db231。

## 决定及范围

用户在收到最终审查结论、计时风险和具体规则草案后提出“能否并入主线？”，
按上下文作为要求完成主线合入处理。本次采用有限工程接受路线，落实
EVALUATION三处既定修订，不追认原统计性能条件通过，不自动接受其他优化。

仅合入B1a：MtpForward可选max_position，multi草稿及非终止extend传入
实际逻辑位置最大值；其余调用保留fallback。实现提交1c6e792，审查提交
e9512d7；原主线867b8a6041af73b59863407d2bb9ab2ea0fb2d7b。提交历史与
原始证据随PR保留，撤回的300请求方案保持WITHDRAWN，不执行B1b。

## 验证与已接受风险

复用[最终审查](MTP_POSITION_B1A_FINAL_REVIEW_2026-10-18.md)的原始身份与
证据：零警告构建、9/9直接合同、56组同形状位置比较、候选HTTP两模式
质量各11/11和同期AB60五档，三份清单1136条文件记录已核验。
本次仅文档规则和合入操作，运行时与测试源文件未改变，不重跑推理。
公共host检查仅证明host合同，不替代本机Thor证据。

未单独强制a=2、未做完整step双二进制全状态差分；有限改动的依赖审查
判断不构成关键遗漏，依据及边界见最终审查。no-MTP 200K decode观察
下降0.2526%，45K下降0.0730%；最大TTFT相对增加0.2302%，最大绝对
增加75.12ms。没有可信区间约束真实退化上界，未证明这些差异全是噪声；
在正确性证据、改动价值及回退能力基础上承担该不确定性。

## 回退与部署边界

独立5文件反向补丁已通过隔离index验证，见
[补丁](evidence/b1a-final-review-20261018/implementation.patch)。可仅撤销
实现/测试改动而保留规则和历史证据。源码回退需构建；运行回退使用保存的
A二进制及匹配配置/缓存重启。当前build/q4t仍为A SHA256
8685d8bf4068b81df0f5162d6c55924d93eae793d17664a1b465ca7587d6c6ee。
本次不替换二进制、不启动服务、不新增模型/HTTP/GPU采样。

## 分支整理范围

采用保留历史的merge提交，使实现和证据原提交可追溯。合入后核对
main包含工作分支全部提交、工作区干净和无活动PR，再删除重复本地分支。
远端仅处理codex/mtp-position-b1a，核对预期tip后删除；不清理其他分支。
.q4t-work、build及依赖原路径的证据全部保留。合并及清理实际结果另记。

## 实际合入与分支整理结果

PR head为8aadc3844ce8cc1aa0af6a1862bb430c0c513c70，base为867b8a6。
PR显示MERGEABLE/CLEAN，两个host-contracts检查均SUCCESS后，以精确head
匹配完成merge。主线包含head及全部阶段历史，没有squash丢失证据锚点。
[PR检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/39104682381)
仅作为公共host验证，原本机Thor证据继续按已记录身份复用。

切换main并快进后确认工作区干净、无该head的活动PR、main完整包含
8aadc3844ce8cc1aa0af6a1862bb430c0c513c70；删除本地重复分支。
远端核对同一tip后通过精确force-with-lease删除仅
refs/heads/codex/mtp-position-b1a。保留锚点为main合并提交c3f3c45，
原工作目录与全部.q4t-work证据保持原路径，其他worktree未改动。
本次文档收尾提交直接记录在main；无新增运行时变更、采样或部署。
