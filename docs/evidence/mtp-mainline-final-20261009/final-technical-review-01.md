# 最终技术准入独立审查 01

审查者：completion_validation_review。2026-10-09。只读审查源码、历史
冻结合同、已有原始结果与身份；本次未构建、运行测试、发送 HTTP 或
加载模型。仅本报告写入工作证据目录，不改源码、索引、分支或原记录。

## 结论

**有条件具备有限 sequential MTP 的主线技术准入条件；当前尚不能把
主线 Goal 判为完成。** 除已安排的最终 30 请求矩阵、原始结果复核、
证据封存及实际主线合入外，未找到原阶段目标中另一项实际未满足的
运行时要求，也未发现需要新增性能候选、扩大清单或重跑全部旧 115
请求的具体理由。最终矩阵尚未在本报告中验收，以下结论以其通过原
冻结检查、实际成本得到披露及封存身份匹配为条件。

正式能力可以是：指定 Thor/checkpoint 的 HTTP 文本 greedy、S1、
slot0、k3、208896/8192、显式开启的 sequential MTP。裸 --mtp 进入
该 verifier，普通服务默认仍关闭 MTP。它真实执行草稿、接受/拒绝与
普通 B1 target，不要求本阶段快于普通生成。快速 T4 仍实验；本轮
完成/终止修复不等于 T4 无损、完整状态或全部数值准入。

## 原目标到当前证据的核对

| 原阶段要求 | 当前满足方式及边界 |
|---|---|
| 有效功能、正常入口、有限支持 | server options resolver 与 generation 实际路径一致；CLI generate、多序列和媒体 MTP 在加载前拒绝。M1 23 host、12 拒绝及 8 HTTP 属于 M1；默认 verifier 与本轮最终源码相同。配置匹配日志不认证任意权重内容。 |
| target、接受前缀、correction、next seed | sequential 生产 TU 直接调用普通 ModelDecodeBatchMulti/B1 和同一 argmax；旧控制、直接状态与 HTTP 证据按来源保留。最终生产控制 3 项通过；新增结果校验只收紧既有合法返回合同。 |
| recurrent/PLE、有效 KV/indexer、位置/history | 旧严格短/长状态对照与 future 不可见性补件保留原身份；核心 writer/reader、target、reset/end 和 strict 状态所有权未改。M2 共享位置复制确有改变，由实际 CUDA 18 例及新的生产 sequential HTTP、最终矩阵承担接线补验，不能说整个 strict 实现未变。 |
| 输出/EOS/上下文尾部 | strict 原有 full-step 条件仅共用命名，真值不变；最终结果边界检查先于输出。T4 新 stop 先于 draft 比较、按可达前缀 restore、terminal 不 extend、T_ext=0 与普通尾部已独立审查。新 9 条受控 HTTP 属于变体证据，不冒充自然 EOS 质量。 |
| 资源失败、取消恢复 | strict reserve/fallback、FinishSequential、RequestControl 与释放机制不变，旧实际同模式 fresh/recovery 证据可复用。M1 另修并验证 prefill 停机入队/等待；M2 四种 T4 逻辑故障每次完成排空、无 fallback、Failed→Idle，恢复精确匹配同模式 fresh。GPU 致命错误及任意异常恢复不在合同。 |
| 代表性质量与数值分开报告 | 原冻结 8 题持续生成及其他质量组保留；4 项双方共有语义不足明确未通过。精确配对是模式一致证据，不是整体模型任务质量保证；不得删除不足或声称全部质量合格。 |
| 五档性能与成本 | 最终普通/sequential 各五档三次共 30 已冻结且正在推进；须以原规则完成、复核并披露。旧成本不标成新 binary 实测，不因 sequential 较慢自行新增优化或有利采样，也不宣称相对旧 main 无回退。 |
| 可维护测试与合入依赖 | 常规 sequential control、CUDA position copy 可重复；故障/终止服务器只在独立 EXCLUDE_FROM_ALL 目标包裹生产 imports。历史失败断言原样保留在诊断目标，完整私有/媒体 q4t_tests 未宣称全绿。foundation 两个独有提交通过实际双亲合并保留，运行来源与工具不变。 |

依据是 docs/MTP_RELEASE_PLAN_2026-10-08.md、MTP_STRICT_BASELINE、
MTP_STRICT_RESULT、MTP_MAINLINE_READINESS、MTP_COMPLETION_FIX、
最终 MTP_MAINLINE_RESULT 草稿及 tools/release/README.md。还核对了
README/文档支持说明、main→当前提交的源与构建变更、最终复用审查、
foundation 集成审查以及本人的 M2 静态/原始 HTTP 审查。

## 本轮独立审查与旧证据复用

本人的 completion-validation-static-review-01 覆盖真实 call 点、
同线程/同流完成、上传源生命周期、结果 sentinel、Failed→Idle 与
terminal 生产五文件。原先仅检查 demangle 无法证明 raw linker 名字；
首构建因此失败的原件保留。之后 wrapper-symbol-independent-review-01
以实际 nm 定义/import/export 核对全部 10+9 个 raw aliases，与 CMake
精确一致；修复仅影响两个变体及其链接设置。

本人 completion-http-independent-review-02 直接读取 9+1 HTTP 原始
记录，585/585 项通过，421 文件绑定；没有以重新运行同一个 parser
作为独立结论。四个故障均在真实操作之后注入，observer 不补做修复
同步，所有失败无成功 finish/usage/text、无普通 fallback；四恢复
与 T4 fresh 全文、计数、finish、路径一致。单条最终生产 sequential
与旧 M1 全文相同。新 SSE 原始 usage 可见；旧 M1 只有客户端数据库
计数，其未保存的 usage-only 块不能追认。

最终复用审查 02 的差异清单指出，原 strict→最终 130→132 项运行
来源中 118 相同、12 修改、2 新增；并不宣称 binary 等价。模型/PLE/
quant/server 归档发生变化，未变化的 target/state/cancel 机制按依赖
复用，新增位置复制及 T4 边界由本轮直接和 HTTP 检查补足。这一范围
与本人对生产变更的审查相符；无法据归档 SHA 不同单独推出必须重跑
全部 115，也不能仅据相同 TU 推出共享依赖完全不受影响。

历史长组提前 EOS 覆盖失败、末项 tail 覆盖失败与定向补件均保留。
真实完整 GPU extend 自然覆盖形状 1/4，2/3 属于生产 TU 有界控制，
是测试前已声明的有限范围；不能写成四形状均完整真实 GPU 验证。
旧 115 是 105 生产+10 变体，48 对比较仍属于原受测候选，绝不成为
最终 binary 的 115 次重新运行。没有用新的成功改写原失败。

main→当前分支含历史优化、trace、量化实验 API 和大量工具/证据，
不是 foundation-only 最小补丁。默认普通/B1 算术并未因 T4 的可选
batch-gather 自动切换；相关环境覆盖仍不获有限正式支持。保留这些
已审查、显式受限的研究代码不另构成“必须开新清理项目”的阻碍。
本报告是有限路径和交付条件审查，不是全仓库无缺陷证明。

## 剩余有界清单

1. **完成并独立复核既定 30 请求。** 只处理实际失败；检查固定输入、
   实际 plain/sequential 路径、输出/finish 配对、完整五档及每次结果，
   记录当前成本、初始化与覆盖限制。复核原 DB/日志和结束/清理状态。
   evalscope 若未留原 usage 块，只能报告 DB 计数，不能补写不存在的
   服务原始证据；不为该工具局限新增请求。
2. **封存当前证据并收敛最终文档。** 最终报告、STATUS、日志和索引
   要给出当前支持边界、成本、旧复用/新补验、所有失败与未覆盖项，
   避免历史“剩余”段落被当成当前开放缺口。保存输入、工具、来源、
   产物与清单 SHA，复核包与原件一致。文档或打包不改变运行身份时，
   无须为整理重跑推理。
3. **检查最终待合入头和依赖。** 文档封存提交后复核 main 基点、
   最终 diff、foundation 已在祖先链、生产来源/二进制及保护工作区
   身份；如仓库分支保护要求检查，按该实际要求处理。只针对新改动
   或具体失败补验，不把旧 CI 或 PR ready 自动当作最终代码证据。
4. **完成主线动作并如实记录。** 技术条件闭合后形成可审查结果；
   若最终 main 合并仍需用户决策，报告“具备合入条件、尚未合入”，
   保持总 Goal 未完成。实际合入须记录 main 提交、依赖与回退点。
   不部署，不自动默认开启 MTP，不删除历史工作分支。

tools/release 的 30 项精确 host gate 仍是 text-v1 受控部署的独立
必要条件，清单与执行器相对 main 未改。本次明确只交付代码合入，
不改 text-v1 包也不扩展其部署资格，因此不能把重新部署验收当作
本轮额外工作；同时不得用本次 23/5 项定向 host 数量冒充新的完整
30 项发布门禁通过。完整模型、所有硬件、默认启用、T4 无损资格、
全面提速均不属于本阶段完成的附加条件。

## 本次审阅身份

- 审阅时间 UTC：2026-10-09T07:56:08.135757+00:00
- main：`8cd297b8af62701b00831ac44704e6b0691753d9`。
- 研究头：`d621d00b311effd83088b5ea6322553e0f94550a`。
- main→研究头 src/include/CMake/cmake/tests/tools binary diff SHA256：`20499867a50fd0d66f4fa9974a36c322ac541a591825657b3becf9270f80d210`。
- 最终生产 q4t SHA256：`eae6949bb275e9c97c3e37c505eddc0988f585daaab01ec3b587c02b80cf5872`。
- 最终报告草稿与 docs/README.md 当前为父 agent 正在整理的未提交文档；本报告不误称工作树全净。

- `docs/MTP_MAINLINE_RESULT_2026-10-09.md`：`8ff8c2784f3d1a7968857880e6fa620e63123d53ea782c79e27b0bffb8630829`。
- `docs/MTP_RELEASE_PLAN_2026-10-08.md`：`25c29322f1357c585cece1cb2f84fd6fafb5c87385abbbbdb8266bced74dd09d`。
- `docs/MTP_MAINLINE_READINESS_2026-10-09.md`：`1133d738d3265ca7d784fb8e6748872ca75c707b0e10240e598e3f62cd46fbc3`。
- `docs/MTP_STRICT_BASELINE_2026-10-09.md`：`e59767a9ad89e0f20877ef116908ab43c48cce558c4ed894506757ede893e66b`。
- `docs/MTP_STRICT_RESULT_2026-10-09.md`：`374115f479c6bf50b3bbf4fbcd81cf0635ff4a126f5c2ab7f4014a58b0927f00`。
- `docs/MTP_COMPLETION_FIX_2026-10-09.md`：`615c39ab1d6bbf0812c0c2f5a7a5fcf57ac0e1403fac97179aa0a6362abacba6`。
- `tools/release/README.md`：`79cc336c191f38a10241dc4ab8340556133bddbfc5339a27f75083ed3dca17e9`。
- `.q4t-work/mtp-mainline-20261009/final-reuse-independent-review-02.json`：`538933ee28b347990c638f4477d0aecd73bf4d30754d4fb4c53355a041ce154c`。
- `.q4t-work/mtp-mainline-20261009/completion-http-independent-review-02.json`：`f7a4149d09776a532264fb3e83988319f9bda179540df9c2a392b4050317ae67`。
- `.q4t-work/mtp-mainline-20261009/terminal-independent-review-02.json`：`7a326577095211bbca9c4f7b0b8b267c6787e5ac0faefae2eca33cc1ab8ec885`。
- `.q4t-work/mtp-mainline-20261009/foundation-merge-independent-review-02.json`：`2ad80a08fd21389315e4e7a5d38ebf8ff783c26a13dbae3cfd0286099ce5b0e7`。
