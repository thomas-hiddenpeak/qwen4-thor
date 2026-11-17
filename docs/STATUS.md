# 当前状态（2026-11-17）

Serve总目标仍为 **active**。按[完整计划](SERVE_MASTER_PLAN_2026-11-08.md)
和[逐项任务表](serve-master-goals-v2.json)推进全部33项任务；阶段验证与
发行是实施边界，不能替代剩余能力。非性能改动仅做受影响行为的针对性
验证和必要业务短验，长稳实跑由用户负责。

## 正在推进

[原生转录直接HTTP/SDK与隔离恢复已分项接受](evidence/serve-asr-http-20261117/README.md)：
复用已成立的4创建，续验5创建通过；中英文文本完全一致、英文WER=0，
实际概率/usage、流式终态、坏参数、取消及同PID Chat均通过。接受
观察为2启动/9创建/93.585秒；含原失败累计4启动/11创建/170.949秒。
原FAILED保持，未跑完整质量/性能。默认rev266及day身份不变。

服务包ASR模型身份、显式资源预算/CLI、配置schema与原生OpenAPI
已实现并完成定向CPU。正在接入主轻/版本恢复：固定实际模型和
音频业务资格，允许显式预算变更，自动恢复不得丢失转录或返还
已消费额度。[13项CPU通过](evidence/serve-asr-recovery-cpu-20261117/README.md)
（0.865秒、0模型动作），继续2启动/12创建/900秒实际主轻组合，
随后原生TLS/独立客户端、版本恢复及可回退交付。
[11项针对性CPU已完成](evidence/serve-asr-package-cpu-20261117/README.md)，
含原失败12次/0.785秒，0模型动作；只补实际config暴露的媒体字段
schema遗漏，原失败保留。继续恢复合同，再冻结下一组GPU组合。实际
对齐/字幕/说话人/VAD、Chat音频、语音输出及全部master继续。

前序证据按未变身份复用：[34项服务CPU](evidence/serve-asr-service-cpu-20261117/README.md)、
[3项传输修复](evidence/serve-asr-transport-20261117/README.md)、
[scope接线修复](evidence/serve-asr-scope-20261117/README.md)、
[3项SDK数值复核](evidence/serve-asr-sdk-values-20261117/README.md)、
[18项资产](evidence/serve-asr-assets-20261117/README.md)、
[21项codec](evidence/serve-audio-codec-cpu-20261117/README.md)及
[24项基础CPU](evidence/serve-asr-foundation-cpu-20261117/README.md)。
[原计算logits差分FAILED](evidence/serve-asr-compute-20261117/README.md)不改写，
后续实际算子复核与API业务接受分列，不宣称完整模型正确性。

PR43在b548eb56的CI发现工作流仍使用已改名的v19用例；已同步
current选择器并通过精确1项CPU（0.455秒）。原失败保留于
[evidence](evidence/serve-pr43-ci-selector-20261117/README.md)。最终head两组
CI通过，[已实际合main](evidence/serve-pr43-merged-20261117/README.md)
（`d15498f1`）；继续真实音频后端和全部master剩余任务。

## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev266；服务包 `82e625f293255134244fe8dd2222943a51a604cdc67e583b20ec3ebf5721dee2` |
| Generation | `e88291fa12a36bed2404e43835bc1eda51ffcc775d0e21dee3d488d6f3e0d0db` |
| 引擎 | `7f25058dbfa3163e7faf17280c72cdfe81c0046c090127cf1c4f438aec444376` |
| 引擎发布记录 | `be524fc27fdf626b4bee5173f092edd464c5a3d9678a233704bd49c95e283806` |
| Guardian | `e8290fa1dedf4f5bddfdb1c09d3ed5167ea9333ebdd8739c86a6affc38c4176a` |
| 原生入口 | `8c669d808962d74612c1ae2e6cee7758d2c19bfba6bf1c938a582d69aa91585a` |
| 服务状态 | `STOPPED`；原生外层监护已配置enabled，累计启动0次 |
| 默认容量 | 单流、ctx8192、prefill2048、输出256、MTP k=3；inflight8/queue2；媒体已启用 |
| 人工整包回退 | rev260根 `168477d190e4b0d2c5f2f92410722b39486801500ad4397ff1c717019f022d64`；原周期/原生入口已验；旧包无新增维护子命令 |
| 自动payload回退 | 已验旧控制/当前引擎包 `087c383b36060e60ce1a2ddcb515b157524d244366cfb7a10a5d844e47df95a0`；不是独立旧引擎回退 |
| 外部依赖 | 默认原生入口及自监护不要求nginx/systemd；视频codec按请求在隔离worker加载；数据源/凭据与TLS/CORS按部署配置 |

[rev266原生维护交付](evidence/serve-native-maintenance-delivery-20261117/README.md)
15项STOPPED发布/实际整根回退/再前进通过，196.492秒、0GPU。
旧rev260自己的check/version-status通过，原周期和全部预算保持；
最终默认已包含三类维护和旧计划恢复。引擎/协议/runtime/native/client/
profile未变。rev266用户day已prepare/check且未运行；PR43最终head两组CI通过且
[已实际合main](evidence/serve-pr43-merged-20261117/README.md)。继续其余全部master任务。

前驱[rev260客户端回执交付](evidence/serve-client-receipts-delivery-20261117/README.md)
已完成13项STOPPED检查/实际回退/再前进，另保留1次意图容量失败，
累计178.267秒、0GPU。精确客户端封装已验，模型/控制/native
和profile保持；128条意图容量通过受保护、可恢复归档4条解决，
其余状态字节和原周期保留。rev260独立day已prepare/check且未运行，继续本组实际合main及全部master。

前驱[rev254有限透视交付](evidence/serve-video-projective-delivery-20261117/README.md)
完成19项STOPPED发布、实际整根回退与再前进，285.699秒、0新增
GPU。原预算/周期保持，控制器/native helper/client未变；原244包、
历史失败及长稳计划保留。用户day独立交接已prepare/check且未运行；PR42两组检查通过且
[实际合main](evidence/serve-pr42-merged-20261117/README.md)（`431ddc13`）；全部master继续。

前驱[rev244仿射与视频细节交付](evidence/serve-video-affine-delivery-20261117/README.md)
完成19项STOPPED发布、实际整根回退与再前进，284.680秒、0新增
GPU。原预算和周期保持，控制器/native helper/client不变；真实视频、
主轻及保留组合已验，原质量失败不追认，原234包及长稳计划保留。
用户day独立交接已prepare/check且未运行；PR41两组检查通过且
[已实际合main](evidence/serve-pr41-merged-20261117/README.md)（`6e976419`），全部master继续。

前驱[rev234原生网页交付](evidence/serve-web-search-delivery-20261117/README.md)
完成19项STOPPED发布、实际整根回退与再前进，284.889秒、0新增
GPU。原预算和周期保持；搜索策略显式启用，原生helper/live控制保持。
真实网页、主轻及保留组合已验，原失败和独立接受分列；原224包及
长稳计划保留；当前day独立快照已prepare/check、未运行。继续实际合main与全部33项剩余任务，
不把有限网站索引等同全网提供方完备，也不把本阶段当总目标完成。

前驱[rev224 Stop组合交付](evidence/serve-stop-combinations-delivery-20261117/README.md)
已完成19项STOPPED发布、真实整根回退与再前进，284.407秒、0新增
GPU。完整可见终态/receipt、prompt-only私有状态、实际工具/图片及
主轻/保留组合已验；原预算与周期保持，原失败如实保留。用户day
新独立快照已prepare/check且未运行，原rev214计划保留；PR38及本组实际合main继续。
全部33项master仍是总目标完成条件，其余协议/模型/资源缺口继续实现。

前驱[rev214媒体缓存交付](evidence/serve-media-state-cache-delivery-20261117/README.md)
已完成19项STOPPED发布、实际整根回退与再前进，283.040秒、0新增
GPU。原视频数值回归已修复，图片/schema/私有缓存、主轻及保留包真实
业务通过；发布模块仅补显式能力迁移，原预算和周期保持。PR38最终
检查与实际合main继续；用户day独立快照已prepare/check、未运行，rev204原入口保留。
全部33项master仍是总目标完成条件，其他缺口继续实现。

前驱[rev204审核交付](evidence/serve-chat-moderation-delivery-20261117/README.md)已完成18项
STOPPED发布、实际整根回退与再前进，229.152秒、0新增GPU。
输入/输出真实审核、SSE门控、存储及双客户端一起交付；原预算和周期
保持，profile仅增加已验能力/身份。PR37两组检查通过且[已实际合main](evidence/serve-pr37-merged-20261117/README.md)
（`c2a3128d`）；用户day独立快照已prepare/check、未启动。全部33项master保持active。

前驱[rev196引用交付](evidence/serve-chat-annotations-delivery-20261117/README.md)已完成18项
STOPPED发布、实际整根回退及再前进，228.641秒，0新增GPU。
引用历史/原字符偏移进入模板，存储和双客户端往返可用；核心/client/
outer预算保持，profile仅做已验能力扩展。原记录和旧包保留。
PR36两组检查通过且[已实际合main](evidence/serve-pr36-merged-20261117/README.md)（`a8238ce4`）；
rev196用户day交接已prepare/check且未启动。当前审核客户端改动须在
下一交付后重新冻结交接，保留原计划；全部master继续。

前驱[rev188运营指标交付](evidence/serve-ops-v5-delivery-20261116/README.md)完成13项
STOPPED发布/实际回退/再前进和入口检查，172.414秒，0新增GPU。
只更新Python对当前上传/tunnel计数的解析；核心/client/outer/profile及
所有本机程序和容量保持。原生60秒/3组/15业务加自检与完整清理通过；
[24小时交接已准备](SERVE_NATIVE_SOAK_HANDOFF_2026-11-16.md)，实际day由用户执行。
本组[PR35已实际合main](evidence/serve-pr35-merged-20261117/README.md)（`38451306`）；R07执行前按当前默认刷新冻结包，维护交接未答
不干预相机/监控。R08及全部协议/媒体/后端master任务继续。

前驱rev182[原生整根回退交付](evidence/serve-root-delivery-20261116/README.md)完成18项STOPPED发布、实际回退再前进及
入口检查，216.388秒，0新增GPU。迁回该根最后持久化的原周期，
保留已消费fallback、故障、selected、terminal及预算，不以旧seed返还
额度；只递增全局revision。现代保留根与候选均有outer和迁移合同。
Guardian新增真实恢复等待角色/原因/剩余时间，预算及核心保护不变。
core/client/outer/profile字节、引擎、入口、设备辅助程序及容量保持。

[真实业务分项接受](evidence/serve-root-rollback-business-20261116/README.md)：
9启动/18Chat含自检/1探测、938.526秒，原消费周期/工具续答/再前进、
精确新保留根outer恢复和候选健康均已验；871项NVML正常，清理通过。
三个原失败驱动保留，不能称原连续链通过。原默认rev174及状态备份、
退休登记/发布副本归档保留。本批[PR34已实际合main](evidence/serve-pr34-merged-20261116/README.md)（`9956e882`）；R07/R08冷启动/
资源硬边界及宿主组合、其余协议和全部master继续。

前驱rev174[Chat标准缓存交付](evidence/serve-chat-cache-delivery-20261116/README.md)
保留其20项C++、8种真实业务、精确主轻及版本恢复证据。模式/TTL、
内容断点、MTP/target状态恢复、JSON/SSE与客户端/key隔离已验，媒体
状态缓存仍须实现。已通过PR33实际合main（`be93d566`），
[合并回执](evidence/serve-pr33-merged-20261116/README.md)保留。

前驱rev166[归属/default交付](evidence/serve-input-attribution-delivery-20261116/README.md)
已通过PR32实际合main（`14221d39`），[合并回执](evidence/serve-pr32-merged-20261116/README.md)保留；
后续缓存/留存改动继续单独提交，全部master推进。

前驱rev158已通过PR31实际合main（`8015dbc8`），
[合并回执](evidence/serve-pr31-merged-20261116/README.md)保留；
[原合同交付](evidence/serve-chat-style-delivery-20261116/README.md)仍有效。
前驱rev150显示几何已通过PR30合main（`d2524e4a`），
[合并回执](evidence/serve-pr30-merged-20261116/README.md)保留。

八云提供方原生只读适配和分页/内容映射已做CPU合同，真实账户业务
仍需部署方配置，未声称通过。任意仿射/其余媒体格式、缓存/分块
prefill、完整客户端矩阵及其余master缺口继续实现；不宣称完整OpenAI
兼容、硬解码、视觉并发或长稳通过。前驱rev142已通过PR29实际合main
（`692982e7`），[合并回执](evidence/serve-pr29-merged-20261116/README.md)保留。

前驱rev134的MCP发行已通过PR28合main（`35581f98`），
[合并回执](evidence/serve-pr28-merged-20261116/README.md)保留；
[原发行证据](evidence/serve-mcp-delivery-20261116/README.md)按原身份复用。

PR #22基础已合main（`ea0ad39a`）。PR #23的`17d8dbe2`两组CI全部通过，已实际合main（`7d00316a`），
[合并回执](evidence/serve-pr23-merged-20261113/README.md)保留。其后namespace
提交也已实际合main（PR24，`79f6e391`），
[合并回执](evidence/serve-pr24-merged-20261113/README.md)保留。
异步实现已实际合main（PR25，`4e87a03f`），
[合并回执](evidence/serve-pr25-merged-20261113/README.md)保留。前驱rev118的
工具发现已发行并实际合main（PR26，`090f0d48`），
[合并回执](evidence/serve-pr26-merged-20261113/README.md)保留；程序调用已在rev126交付并通过PR27实际合main；全部master继续。

## 最近完成的实现和验证

[PR43两项失败夹具已针对性修正](evidence/serve-pr43-ci-fixtures-20261117/README.md)：
普通I/O等待不再与100/250ms调度余量绑定，目标故障和业务断言保持；
拆开复合断言以定位子条件。本机2项1.115秒通过、构建零警告，生产
代码/时限未变；最终head两组CI已通过；PR43包含意图容量修复和原生发行入口，
已实际合main（`d15498f1`），全部master继续。

[rev266原生维护交付](evidence/serve-native-maintenance-delivery-20261117/README.md)
15项STOPPED发布/实际整根回退/再前进通过，196.492秒、0GPU。
旧rev260自己的check/version-status通过，原周期和全部预算保持；
最终默认已包含三类维护和旧计划恢复。引擎/协议/runtime/native/client/
profile未变。rev266用户day已prepare/check且未运行；PR43最终head两组CI通过且
[已实际合main](evidence/serve-pr43-merged-20261117/README.md)。继续其余全部master任务。

[原生维护精确包与历史计划实际验证通过](evidence/serve-native-maintenance-package-20261117/README.md)：
28独立CPU/32次执行，34条原生命令374.106秒，0模型动作。三类
旧计划原样恢复并重新归档，当前122意图/30目录/14登记；原驱动
失败和全部原计划保留。最终v20包已固定，继续15项STOPPED发布/
旧入口回退/再前进；rev260尚未变更，全部master继续。

原生维护16条实际命令通过：三类归档/恢复和安全预留容量完成，
保留驱动一次意图列表断言失败及其续做。发现并补齐旧计划来源兼容，
[7项针对性CPU通过](evidence/serve-native-maintenance-legacy-plans-20261117/README.md)：
精确两代源码生成计划由新CLI原样恢复，未知来源仍拒绝。下一步
新精确包真实旧计划恢复/重新归档及可回退发行，rev260保持，0GPU。

[原生维护25项CPU通过](evidence/serve-native-maintenance-cpu-20261117/README.md)：
38.908秒，13项新增/12项直接回归，0GPU。三类CLI实际文件事务、
持锁身份/保护、历史包及隔离导入已验；精确包和实际发行尚待完成。
默认rev260保持，下一步密封v20及STOPPED可回退交付，全部master继续。

[原生保留管理交付计划](SERVE_NATIVE_MAINTENANCE_2026-11-17.md)已冻结：
v20密封包/统一CLI覆盖三类容量和身份保护，历史v19核验保持。
先针对性CPU及STOPPED交付，模型与native helper不变，不新增GPU；
PR43修复head独立检查，全部master继续。

PR43首次两组CI在来源库存检查失败：新增意图归档模块缺少审查锚点。
已按实际代码/8项CPU证据补入唯一缺失哈希，本机库存及7项变异检查
通过；保留原CI失败，继续最终提交检查和实际合main，未把失败当完成。

发行入口审查确认：意图归档目前在仓库工具中，尚未进入rev260密封
服务包/原生service子命令。N07保持进行中，下一子任务补齐包和CLI，
并保持旧包核验与全部预算；不能把开发脚本可用当正式入口齐备。

[rev260客户端回执交付](evidence/serve-client-receipts-delivery-20261117/README.md)
已完成13项STOPPED检查/实际回退/再前进，另保留1次意图容量失败，
累计178.267秒、0GPU。精确客户端封装已验，模型/控制/native
和profile保持；128条意图容量通过受保护、可恢复归档4条解决，
其余状态字节和原周期保留。rev260独立day已prepare/check且未运行，继续本组实际合main及全部master。

已解决的本组原失败：客户端交付在rev256回退步骤遇到发布意图128条容量上限；
原失败保留、服务STOPPED且无GPU动作。补充可恢复意图归档：
最多4条显式选择，[8项针对性CPU通过](evidence/serve-publication-intent-cpu-20261117/README.md)，
严格共享锁和待恢复/当前历史保护；
实际归档后续9项检查已通过，累计13项成功命令及1次原失败，不重跑客户端用例。

[客户端回执修复计划](SERVE_CLIENT_RECEIPTS_2026-11-17.md)已冻结：
补Node SDK桥接丢失审核字段、恢复器丢失审核/logprobs及被拦截终态。
[12项新用例及4项直接回归已通过](evidence/serve-client-receipts-cpu-20261117/README.md)，
原10处失败断言保留，无新增GPU；[精确包12项亦通过](evidence/serve-client-receipts-package-20261117/README.md)，
继续STOPPED可回退发行，
PR42冻结head继续检查和实际合main，
全部master保持active。

[rev254有限透视交付](evidence/serve-video-projective-delivery-20261117/README.md)
完成19项STOPPED发布、实际整根回退与再前进，285.699秒、0新增
GPU。原预算/周期保持，控制器/native helper/client未变；原244包、
历史失败及长稳计划保留。用户day独立交接已prepare/check且未运行；PR42两组检查通过且
[实际合main](evidence/serve-pr42-merged-20261117/README.md)（`431ddc13`）；全部master继续。

[有限透视发行组合通过](evidence/serve-video-projective-composition-20261117/README.md)：
累计5启动20请求（含自检/embedding），235.266秒严格租约，
223项NVML正常、完整清理。主轻及两个精确保留包实际业务均通过；
显式能力迁移保持预算/历史，禁止回退丢能力。默认244保持，
继续STOPPED可回退交付、实际合main及全部master。

[有限透视真实业务接受](evidence/serve-video-projective-business-20261117/README.md)：
1启动4生成、49.230秒严格租约，47项NVML正常、完整清理；非单位w
JSON、透视SSE、删除原上传后的冻结历史、奇异请求后同PID普通
文本42均通过。默认rev244保持，继续主轻/保留组合和可回退交付；
保留服务驱动4项CPU通过。PR41已实际合main（6e976419），全部master仍active。

[有限透视CPU接受](evidence/serve-video-projective-cpu-20261117/README.md)：
16项新CPU及4项直接回归全过，1.038秒；独立有理数逐像素、真实
H264/PAR、源/冻结校验和清理已验。两项驱动与库存检查通过，构建
零警告，原测试字段名构建失败保留。默认rev244保持，继续1启动
4生成600秒业务、适用发行及实际合main；全部master仍active。

[齐次/透视视频下一组合同](SERVE_VIDEO_PROJECTIVE_2026-11-17.md)已冻结：
补合法非单位w和有限透视的实际像素/冻结重放，用有界整数计算保持
确定性且不新增依赖。已接入隔离worker、来源绑定和v5冻结重放，并生成独立Fraction/
Gauss-Jordan像素oracle。先16项新CPU及4项直接回归，尚未测试；rev244
保持，PR41只推进已验提交，全部master继续。

[rev244仿射与视频细节交付](evidence/serve-video-affine-delivery-20261117/README.md)
完成19项STOPPED发布、实际整根回退与再前进，284.680秒、0新增
GPU。原预算和周期保持，控制器/native helper/client不变；真实视频、
主轻及保留组合已验，原质量失败不追认，原234包及长稳计划保留。
用户day独立交接已prepare/check且未运行；继续PR41实际合main及全部master。

[仿射/视频细节发行组合通过](evidence/serve-video-affine-composition-20261117/README.md)：
主轻及修复后两保留控制包均已实际验证；含原失败累计7启动24请求
（含自检和embedding），323.503秒严格租约，308项NVML正常，
完整清理。原auto小尺寸质量失败及含答案Chat题拒答继续保留；没有
用新输入通过追认旧题。控制/native helper/client未变，能力扩展保持
预算和历史且禁止回退丢失新能力。默认234保持，继续STOPPED可回退
交付和PR41实际合main，全部master仍active。

[视频仿射实施合同](SERVE_VIDEO_AFFINE_2026-11-17.md)已冻结：补合法
缩放/斜切/非直角旋转的实际像素处理及v4冻结重放，保留旧八方向。
12项新CPU及4项直接回归[全部通过](evidence/serve-video-affine-cpu-20261117/README.md)，
0.788秒、构建零警告；正在准备最多1启动4请求600秒业务短验。
首条小尺寸模型任务[未通过](evidence/serve-video-affine-small-input-20261117/README.md)，
已捕获真实帧及CPU张量逐位核对正确；继续补显式视频detail传播和
有界细节预算，原auto/low数值合同及失败记录保留。[显式细节8项CPU
及驱动/schema检查通过](evidence/serve-video-detail-cpu-20261117/README.md)，
同源同问题的high实际业务[4项通过](evidence/serve-video-affine-business-20261117/README.md)，
含原失败累计2启动5生成、86.304秒严格租约，83项NVML正常且清理
完整；默认234保持，继续主轻/保留组合、可回退交付及实际合main。
保留包业务驱动3项CPU通过，复用严格生命周期；只验证实际仿射
SSE/存储/客户端隔离和奇异请求后的普通文本，不扩展无关故障矩阵。
主轻2启动10请求已通过；保留驱动隔离预检发现并修复旧包测试夹具
导入，[CPU补验通过](evidence/serve-video-affine-service-preflight-20261117/README.md)，
预检未占GPU。旧包首轮随后1启动3请求[失败](evidence/serve-video-affine-retained-failure-20261117/README.md)：
GET投影脚本已修正，新含答案Chat题的模型拒答仍保留；恢复原中性
Responses题后补两保留业务，累计上限5/19/900。PR41草稿已建，继续精确保留业务及交付，未提前合并。
PR40已[实际合main](evidence/serve-pr40-merged-20261117/README.md)，
一般透视/多轨/其余媒体后端和全部master仍未完成。

[网页输出类型CI修复](evidence/serve-web-search-ci-repair-20261117/README.md)：
旧push的2269项C++仅1项旧枚举断言失败，补齐web_search_call后该项
CPU通过，0.031秒；不改运行时/default234/day，不追加GPU。PR40已
创建，旧提交检查取消，最终head两组通过并实际合main（63190189）；
全部master继续。

[rev234原生网页交付](evidence/serve-web-search-delivery-20261117/README.md)
完成19项STOPPED发布、实际整根回退与再前进，284.889秒、0新增
GPU。原预算和周期保持；搜索策略显式启用，原生helper/live控制保持。
真实网页、主轻及保留组合已验，原失败和独立接受分列；原224包及
长稳计划保留；当前day独立快照已prepare/check、未运行。继续实际合main与全部33项剩余任务，
不把有限网站索引等同全网提供方完备，也不把本阶段当总目标完成。

[网页发行组合接受](evidence/serve-web-search-composition-20261117/README.md)：
含原失败累计8启动25请求、361.781秒严格租约、343项NVML正常。
主轻和两个保留控制包的既有业务、搜索缺配置后同PID继续均已验，
旧包后处理失败保留并单列独立CPU接受，现代包完整通过。默认224
保持；继续STOPPED升级/整根回退/再前进、实际合main及全部master。

网页旧保留包1启动3请求已执行，缺策略503后同模型/控制器完成实际
图片/schema/SSE及存储/隔离；末尾脚本导入旧包没有的迁移模块而FAILED。
修复为核对精确封包的已资格化声明，CPU在同样模块隔离下复核真实
响应/源码/清理通过，原失败不改。原3请求独立接受、0追加GPU；
继续现代保留包未运行的1启动3请求及发行，全部master仍active。

网页发行主轻组合2启动10请求通过，87.694秒，83项NVML正常、清理
完整。候选封包完成，默认224保持；两个保留控制包各1启动3请求，
验证缺搜索策略503后同PID继续实际媒体/schema/SSE业务，原总预算
4启动16请求900秒保持。继续可回退交付、实际合main和全部master。

[网页自恢复接入CPU通过](evidence/serve-web-search-recovery-cpu-20261117/README.md)：
7项针对性用例全过，0.667秒；bundle/CLI、主轻命令与私有策略贯通，
能力迁移不能丢失网页合同或返还已消费预算，自动回退不能改变网络
权限。默认224保持、0新增GPU；下一组冻结4启动16请求900秒的
主轻/保留发行组合，完成后可回退发布及实际合main，全部master继续。

[网页搜索真实业务接受](evidence/serve-web-search-business-20261117/README.md)：
候选e430a4bd的Chat/Responses JSON/SSE、真实来源/引用、离线/历史、
工具选择及缺配置后同PID普通核心健康通过。正式2启动8请求，含原
失败累计4启动9请求、严格租约177.118秒，168项NVML正常且清理
完整；原题修复后通过，未追认旧失败。默认224保持，继续自恢复
整包、主轻/保留版本、可回退交付和实际合main，全部master仍active。

[网页API及客户端CPU接受](evidence/serve-web-search-api-cpu-20261117/README.md)：
30项不同C++/38次执行、4项合同及Python/JS各2项最终通过。修复
SDK选择回显、非流式来源投影、原生多步预算及配置schema漏项，原
失败保留。实际模型尚未验收，0新增GPU、默认224保持；继续冻结
2启动8外部请求900秒真实公网/模型业务及可回退交付，全部master继续。
首轮1启动0生成请求35.193秒在来源预检403停止：DNS映射到保留网段。
原生产网络保护保持；单地址显式私有授权后，原生CPU实际TLS来源/
摘要及离线重读1项通过，0.587秒。继续补尚未执行业务，累计上限
3启动8请求900秒，原失败/清理记录保留。
第二轮来源正确取得，但首条Chat错误拒绝；1启动1请求39.566秒，
普通协议拒绝终态合法不代表业务成功。[网页证据指导与可选封包
13项CPU通过](evidence/serve-web-search-repair-cpu-20261117/README.md)，
候选e430a4bd的同一原题补验已通过，详细结果见上方真实业务记录；
默认rev224保持，发行组合继续。

PR39最终head `0472f354` 两组CI通过，已[实际合入main](evidence/serve-pr39-merged-20261117/README.md)
（`1d905350`）。Stop组合、rev224交付和独立day交接源码已进入
主线；网页搜索及全部剩余master任务继续，0新增GPU、默认224保持。

[原生网页执行CPU通过](evidence/serve-web-search-execution-20261117/README.md)：
16项新合同和4项既有获取合同最终通过，20项不同用例/21次执行。
真实本地HTTP抓取、离线/刷新、来源读取/查找、内容/位置及预算已验；
原预算耗尽仍连接缺陷已修复，原失败保留。0GPU，默认224保持；
继续实际Chat/Responses接入、引用/SDK、公网业务和交付。

[网页持久索引CPU验证](evidence/serve-web-search-index-20261117/README.md)
8项全部通过，6.395秒，零警告构建、0GPU、默认rev224保持。认证
scope/配置身份、跨重启读取、CAS/丢回执不重放、容量/取消与真实
过期已验；继续原生抓取执行及Chat/Responses/引用/SDK，未记完整搜索。

PR38最终head `8f55b643` 两组CI通过，已[实际合入main](evidence/serve-pr38-merged-20261117/README.md)
（`0e4f5ac2`）。媒体状态缓存、修复和rev214交付已进入主线；
已验Stop rev224单独推进下一PR，网页搜索及全部master缺口继续。

[网页搜索网络组件](evidence/serve-web-search-network-20261117/README.md)
8项直接CPU通过：每跳域名限制先于DNS/连接，与部署网络许可取交集；
offline不联网，失败保留尝试预算，合法来源链/摘要保持。0GPU、
默认rev224不变；持久索引及完整工具/引用/SDK接入继续。

[网页搜索组件CPU验证](evidence/serve-web-search-components-20261117/README.md)
19项不同用例/31次执行通过，3次构建零警告，0GPU、默认rev224保持。
真实本地HTTP提取、来源/过滤/离线词法排序及HTML纯内存沙箱已验；
持久索引、Chat/Responses调用与引用/SDK/发行继续，不记完整功能完成。

原生网页搜索实施已冻结[完整合同与有界计划](SERVE_WEB_SEARCH_2026-11-17.md)：
补实际搜索/读取/引用、Chat与Responses执行、离线索引和可选数据源。
当前仍只有类型/include投影，不能记功能完成；先做最多18项直接CPU
组件验证，默认rev224不变。PR38测试时限修复8f55b643独立推送，
最终CI与实际合main继续；Stop改动单独保留，全部master保持active。

[PR38时间夹具CPU通过](evidence/serve-pr38-ci-timing-20261117/README.md)：
7项关联用例、17.257秒，隔离零警告构建、0GPU、生产源码未改。
精确worker次数、不重放、真实过期/回收断言保持，原三项失败保留。
独立修复推送后继续最终head检查与实际合main，后续Stop不混入此PR。

PR38的56477a5a一组CI通过，另一组发现三类时间夹具耦合：正常
缓存worker启动被100ms缩短、MCP一秒TTL在读取前过期、向量任务
正常存储同被100ms限制。隔离工作区仅修夹具，保留故障注入、次数/
不重放/清理/实际过期断言；冻结7项关联CPU、0GPU，生产实现不改。
通过后继续该PR最终检查和实际合main；Stop独立工作未并入本PR。

[rev224 Stop组合交付](evidence/serve-stop-combinations-delivery-20261117/README.md)
已完成19项STOPPED发布、真实整根回退与再前进，284.407秒、0新增
GPU。完整可见终态/receipt、prompt-only私有状态、实际工具/图片及
主轻/保留组合已验；原预算与周期保持，原失败如实保留。用户day
新独立快照已prepare/check且未运行，原rev214计划保留；PR38及本组实际合main继续。
全部33项master仍是总目标完成条件，其余协议/模型/资源缺口继续实现。

[Stop组合真实业务接受](evidence/serve-stop-combinations-business-20261117/README.md)：
累计6启动27请求（含原失败/保留包自检）、严格租约342.380秒，
323项NVML正常且全部清理。可见终态/必需工具、MTP prompt-only
恢复、实际图片/schema/缓存概率对照及主轻/两保留控制包通过；原失败
未追认、EOS续问未冒称stop命中。下一步≤20条STOPPED可回退交付，
0新增GPU；默认214保持，PR38独立检查及全部master继续。

Stop首轮1启动10请求、96.620秒未整体通过：前9条独立核对通过，
MTP实际保存/恢复13361-token prompt且首次停止丢弃隐藏尾部；第10条
固定字符串视觉题返回合法拒绝，脚本错误解析null。保留原失败，
改实际图片内容题，仅补媒体3条；累计6启动27请求1200秒，默认214不变。

[PR38回执恢复夹具修复](evidence/serve-pr38-ci-payload-repair-20261117/README.md)：
一组最终d4e检查通过，另一组正常payload GET超出250ms夹具时限。
恢复默认2秒I/O预算，原实际提交丢回执/精确恢复断言保持；1项CPU
通过，运行时及rev214未变，0GPU。新head CI通过后实际合main。

[Stop组合CPU通过](evidence/serve-stop-combinations-cpu-20261117/README.md)：
18项不同C++/19次、4项迁移/7次、2项schema和2项客户端通过，735/7
来源通过，隔离构建零警告。两类夹具原失败保留并只重验受影响项；
候选尚未GPU验收、默认rev214保持。下一步首组1启动12请求，再按
总上限6启动27请求1200秒完成主轻/保留组合。PR38只接收独立CI修复，
本组先本地提交；网页搜索后端及全部master继续，不能以类型结案。

[rev214媒体缓存交付](evidence/serve-media-state-cache-delivery-20261117/README.md)
已完成19项STOPPED发布、实际整根回退与再前进，283.040秒、0新增
GPU。原视频数值回归已修复，图片/schema/私有缓存、主轻及保留包真实
业务通过；发布模块仅补显式能力迁移，原预算和周期保持。PR38最终
检查与实际合main继续；用户day独立快照已prepare/check、未运行，rev204原入口保留。
全部33项master仍是总目标完成条件，其他缺口继续实现。

[媒体能力迁移CPU通过](evidence/serve-media-cache-migration-cpu-20261117/README.md)：
6项不同用例/7次执行，735/7来源通过。仅重封候选的发布迁移模块；
先由新候选迁移能力，再安装已经实跑的原现代保留根，之后验证两根
回退和再前进，最多20条STOPPED命令、0GPU。实际默认仍204。

发布预检发现实际迁移缺口：媒体缓存/分块false→true及pending→false
被通用“只能增加字段”规则拒绝。现补限定完整媒体身份/位置/边界、
Chat/Responses一致的显式升级语义；不允许自动加能力、降级或任意
布尔翻转。冻结6项直接CPU迁移检查和来源检查，之后STOPPED发布/
回退；引擎及真实GPU业务不变，控制包重封并明确证据复用范围。

[媒体状态/视觉分块业务接受](evidence/serve-media-state-cache-business-20261117/README.md)：
新引擎正式接受6启动26请求；包含原失败/诊断/命令补验累计13启动
60请求、严格租约636.757秒，605项NVML正常，全部清理。原视频
数值回归由原输入修复，图片/schema/私有状态及主轻/两个保留包
通过；旧失败未追认。继续STOPPED可回退交付及PR38合main，默认
仍204、全部master任务继续。

旧控制包本次实际SSE/存储/隔离已返回成功，但验证入口手工命令未
严格使用计划的绝对路径及Python -I，正式postflight拒绝，原记录
保留，不追认通过。现代保留组合改为直接执行冻结command；旧控制
仅补1启动2请求/180秒，总上限明确13/60/1200秒。生产引擎不变，
默认204保持，全部master继续；后续禁止手工重写已冻结命令。

新候选受影响媒体9条及主轻10条已通过：旧全量图片与新分块logprobs
一致，换图未命中/追加命中、schema冷/热和8709-token私有恢复成立；
主轻实际工具/embedding/原视频通过。累计10启动54请求约488.965秒，
环境清理通过；剩余仅两个保留组合2启动4请求后准备发行。
[PR38夹具定点修复](evidence/serve-pr38-ci-repair-20261117/README.md)2项CPU及
隔离零警告构建通过，运行时不变。默认204保持，全部master继续。

PR38旧head的Batch线程恢复CPU夹具在250ms存储截止前超时，随后
cleanup_pending串扰配额用例；原CI日志保留。该用例并非存储故障
注入，现仅把其I/O预算对齐5秒任务预算；冻结该用例及后续配额
2项定点CPU验证，生产运行时和已验GPU身份不变。

[原视频短尾回归已修复](evidence/serve-media-prefill-repair-business-20261117/README.md)：
1启动3请求，完整/隐式冷/热文本及logprobs完全一致，实际1062-token
SSD写入/恢复通过，监控清理正常。新候选2de56309未交付；下一组
5启动23请求/780秒完成受影响图片/schema/私有缓存和主轻/保留组合，
累计上限12/58/1200秒。原失败保留，默认204不变，全部master继续。

[视觉短尾修复CPU出口](evidence/serve-media-prefill-repair-cpu-20261117/README.md)：
2项直接CPU、735/7来源与零警告隔离构建通过。原诊断/来源失败
保留；新候选尚未GPU验收，接下来仅3条原视频对照。默认204保持。

标准视频定位已确认：完整计算正确，隐式冷/热均拒绝；捕获对照中
1062-token前缀全部48层逐位一致，7-token尾部从首层attention输出开始
分歧。原两次定位共2启动5请求93.531秒，累计6启动32请求295.819秒。
现修复视觉prefill短尾的内核分派，显式保留prefill BF16/HC/MoE/QSA
路径；作用域在head前结束，文本decode/MTP不受影响。修复尚未验收。
仅新增2项CPU作用域/线程隔离检查，随后1启动3条原视频对照/300秒；
累计上限7启动35请求，原全部失败及默认rev204保留，交付资格另冻结。

[媒体发行资格回归](evidence/serve-media-state-cache-profile-failure-20261117/README.md)：
同一标准视频请求在隐式缓存后拒绝看帧，主配置业务未通过；候选
5068d4c9不可交付，默认rev204保持。私有8709 token恢复补验已通过。
原4启动27请求202.288秒与清理保留；下一步限定1启动3请求诊断
一次性/隐式冷/热差异，修复后仅重验受影响项，全部master继续。

[媒体缓存原业务复核](evidence/serve-media-state-cache-business-review-20261117/README.md)：
2启动20请求110.884秒，工具正确摘要带说明被驱动误判，原失败
保持；离线核对真实工具与3项负控，已完成分项接受。旧全量/分块及
缓存冷/热logprobs一致，105项NVML正常，全部清理。只补私有会话
2条，明确总上限7启动/36请求/1200秒；不重跑通过项。默认204不变。

[媒体缓存CPU接受](evidence/serve-media-state-cache-cpu-20261117/README.md)：
34项C++/2项schema最终通过，729/7来源和隔离零警告构建通过。原单槽
媒体限制与生成文档失败保留，只补受影响项；候选`5068d4c9`未交付。
继续最多6启动/36请求/1200秒的真实业务，默认rev204与day快照保持。

[媒体状态缓存/视觉分块](SERVE_MEDIA_STATE_CACHE_2026-11-17.md)已冻结实现合同：
实际内容与完整视觉块身份、三轴位置/偏移、模板边界、特征切片及
资源边界一起补齐。最多36项直接CPU，必要6启动/36请求/1200秒；
CPU已通过、真实业务与交付待完成，默认rev204保持，全部master继续。

[PR37 CI定点修复](evidence/serve-pr37-ci-repair-20261117/README.md)：
原CI的MCP视频断言未适配分类后置说明，现核对全部媒体和首尾文本，
1项CPU及隔离零警告构建通过。运行时/默认不变、0GPU；原失败保留，
继续新head检查与实际合main，全部master推进。

[长稳入口已固定到独立快照](evidence/serve-frozen-day-handoff-20261117/README.md)：
589份源码及rev204精确包冻结，prepare/check通过，0模型动作，生产
默认和账本不变。用户24小时可独立开展，后续开发不会改变该计划；
原计划保留。继续PR37实际合main和媒体缓存等全部master任务。

[rev204审核交付](evidence/serve-chat-moderation-delivery-20261117/README.md)已完成18项
STOPPED发布、实际整根回退与再前进，229.152秒、0新增GPU。
输入/输出真实审核、SSE门控、存储及双客户端一起交付；原预算和周期
保持，profile仅增加已验能力/身份。PR37两组检查通过且[已实际合main](evidence/serve-pr37-merged-20261117/README.md)
（`c2a3128d`）；用户day独立快照已prepare/check、未启动。全部33项master保持active。

[Chat审核真实业务接受](evidence/serve-chat-moderation-business-20261117/README.md)：
修复组4启动/17外部请求/238.768秒，通过原输入block、批准SSE、多
choice score、存储重启及客户隔离；内部分类另记12步。原失败保留，
累计8启动/32请求/443.027秒，全部自有资源清理。新引擎`e016bd5f`
和主轻/两种保留包已验，默认仍rev196；继续可回退发行及PR37合main。
全部33项master仍active，不将本阶段接受当作总目标完成。

[分类数据修复CPU通过](evidence/serve-chat-moderation-repair-cpu-20261117/README.md)：
10项直接用例、726/7来源和隔离零警告构建通过；记录仍原样嵌入，
未改阈值或拒绝保护。继续使用原失败输入短验，未声明运行时修复。
默认rev196保持，全部master继续。

[Chat审核原始短验已保留](evidence/serve-chat-moderation-business-failure-20261117/README.md)：
4启动/15外部请求/204.259秒，194项NVML正常，清理完成。主轻资格及
已返回的SSE/存储可复核，但威胁输入的内部模型拒绝分类，外层503，
不能当作成功拦截。候选不交付，默认rev196保持。正在改进分类任务与
引用数据的表达，保持拒绝/错误保护；只补10项受影响CPU和同一失败
输入的必要精确包业务，下一组≤4启动/17请求/900秒，总≤8/32/1200秒。
原始失败不改写；PR37草稿继续，全部master保持active。

[Chat审核CPU接受](evidence/serve-chat-moderation-cpu-20261117/README.md)：
26项C++/8种前端最终通过，726来源/7负控及隔离零警告构建通过。
原编译和前端失败保留；JS合法缺省logprobs误拒绝已修，仅补失败项。
当前引擎候选`23f93435`未交付；继续4启动/18外部请求/900秒真实业务。
默认rev196保持，全部master继续。

[Chat原生审核](SERVE_CHAT_MODERATION_2026-11-17.md)进入成组实现：补标准
score/block、真实输入/全choice输出分类、SSE发布与存储门控和累计
配额/Batch，最多26项C++/8前端病例，必要4启动/18请求/900秒。
默认rev196保持，PR36已合main；阶段结束不停止总计划。

[rev196引用交付](evidence/serve-chat-annotations-delivery-20261117/README.md)已完成18项
STOPPED发布、实际整根回退及再前进，228.641秒，0新增GPU。
引用历史/原字符偏移进入模板，存储和双客户端往返可用；核心/client/
outer预算保持，profile仅做已验能力扩展。原记录和旧包保留。
PR36两组检查通过且[已实际合main](evidence/serve-pr36-merged-20261117/README.md)（`a8238ce4`）；
rev196用户day交接已prepare/check且未启动。当前审核客户端改动须在
下一交付后重新冻结交接，保留原计划；全部master继续。


[Chat引用业务接受](evidence/serve-chat-annotations-business-20261117/README.md)：
4启动/14请求/185.929秒，176项NVML正常且清理通过。主轻真实资料
工具及跨重启存储、旧/现代保留控制包SSE引用标题及客户隔离通过；
当前控制源与helper按rev188精确身份复用。继续现代回退根/候选
可回退发行与PR36合main，全部master继续，默认仍188。


Chat引用主/轻与旧控制器业务完成：3启动/12请求，约141.05秒，
真实资料工具、JSON/SSE、原消息及重启回读、客户隔离通过；清理通过。
原后置命令-I/审计器提前导入失败保留并定点复核，没有重跑业务。
发行前补现代整根回退组合，保持新增引用、outer和周期合同；仅再
1启动/2请求/180秒，累计4/14且GPU总时≤600秒。默认rev188保持。


[Chat引用历史CPU通过](evidence/serve-chat-annotations-cpu-20261117/README.md)：
16项不同C++/8前端病例最终通过；首轮三个模板预期偏差只修断言，
原失败保留。723/7来源及隔离零警告构建通过，能力描述已补。继续
最多3启动/12请求/600秒实际资格、SSE/存储隔离与可回退发行。
PR35两组CI通过且已实际合main（`38451306`），全部master继续。


Chat引用历史已成组实现：shared合同校验UTF-8字符偏移和字段，模板
转义控制标记且保持原消息坐标；统一Chat/vision/message/OpenAPI
schema已同步。存储复用原始记录，新增10项直接C++回归与8个schema/
原生预检/双客户端病例，另复验最多6项受影响旧合同。尚未构建或
运行测试；默认rev188保持，必要业务仍上限3启动/12Chat/600秒。


[Chat引用历史](SERVE_CHAT_ANNOTATIONS_2026-11-16.md)进入实现：补非null
annotations的合同、原assistant模板数据、统一schema和持久往返；
不冒充已执行检索。冻结16项直接C++/8前端病例，必要业务最多3启动/
12Chat/600秒；PR35已创建等待CI，全部master继续。

[rev188交付与day交接](evidence/serve-ops-v5-delivery-20261116/README.md)完成，13项
发布/实际回退/再前进与健康检查通过、0新增GPU；默认STOPPED、原预算
保持。day只读prepare/check通过，未执行。继续实际合main和全部master。

[原生60秒短验通过](evidence/serve-native-soak-business-20261116/README.md)：
新包509c3bcb，3组/15业务生成加自检，3次真实工具闭环及运营指标排空，
105.917秒，清理与完整证据复核通过。含原两失败累计3启动/28Chat/
209.112秒，194项NVML均OK；继续可回退发布、day交接、合main及全部master。

[当前v5指标CPU修复](evidence/serve-ops-v5-cpu-20261116/README.md)：
第二轮五类业务完成后发现旧解析器拒绝五个新标签；两轮原失败保留，
累计2启动/12Chat/103.195秒且清理通过。四项直接CPU与723/7来源通过，
继续精确新包、一次1启动/16Chat/600秒short、可回退发布与全部master。
PR34两组CI全部通过，已经实际合main；后续观测/交接工作单独提交。

[原生长稳指标入口修复](evidence/serve-native-soak-metrics-20261116/README.md)：
首轮1启动/6Chat/51.339秒，五类业务完成后驱动错误端口取指标失败，
原失败与清理保留。复用正确运营入口，两项直接CPU及723/7来源通过；
只补1启动/16Chat/600秒连续short，累计2/22/1200秒；全部master继续。

[原生长稳采集依赖隔离](evidence/serve-native-soak-import-20261116/README.md)：
审查后解除纯日志路径对旧资源验证栈的提前导入；两项直接CPU及
723/7来源通过，0GPU。精确short准备已完成，保留原计划后更新验证
绑定，继续实际sealed入口检查、short及全部master。

[原生长稳交接CPU通过](evidence/serve-native-soak-cpu-20261116/README.md)：
14项首过，密封记录复核后只补1项，723来源/7负控通过。当前包/原生
outer、实际工具及原始wire/清理复核已实现；day每分钟一组保持原配额，
24小时由用户执行。继续精确prepare/check及1启动/16Chat/600秒short。

PR34两组CI均57/58套件通过，仅三个新包用例误留v18断言而失败。
[定点修复](evidence/serve-pr34-ci-repair-20261116/README.md)改为实际v19，
3项CPU首过，历史v18用例保持，运行时/默认不变、0GPU。继续新head
检查及实际合main，不用本机验证替代远端结果。

[Q03原生长稳交接](SERVE_NATIVE_SOAK_2026-11-16.md)进入开发：补当前包
可冻结入口及原生outer生命周期，保留JSON/SSE/schema/真实工具、1h
与24h检查点和独立证据复核。最多14项CPU后只做1启动/16Chat/600秒
short；day由用户执行，旧systemd证据保持，全部master继续。

[R07精确包预检通过](evidence/serve-cold-refresh-preflight-20261116/README.md)：
rev182原包普通prepare及root只读check闭合，原污染失败和plan保留；
2项owner CPU通过。0GPU/0围栏/默认保持，等待相机/监控服务明确
维护交接；期间继续独立协议与交接工作，全部master保持active。

R07精确包prepare通过；管理员只读check在设备操作前发现验证helper
污染sealed namespace。已改为按文件加载，2项原owner降权CPU通过，
保留失败与原plan，仅更新验证文件绑定补check；默认/宿主服务保持。

[R07当前包CPU接受](evidence/serve-cold-refresh-cpu-20261116/README.md)：
5项不同用例通过，原3项异常类型断言错误保留，仅重验受影响项。
宿主只读观察BUSY：nvargus-daemon/jtop持设备句柄；未安装围栏或
干预其他服务，0GPU。继续精确包准备/只读核验及其他独立master任务。

[R07当前包冷启动](SERVE_COLD_REFRESH_2026-11-16.md)开始：旧准备器固定
过时构建/证据已改为精确封包及完整来源核验，复用rev182原件。冻结
5项CPU+2项实际封包只读准备/核验，随后1启动/3Chat/1探测/600秒。
内层NVML故障注入、外层真实严格监控和内核准入保持；宿主忙不干预。
PR34已创建等待检查；全部master继续，当前默认保持STOPPED。

[rev182原生整根交付](evidence/serve-root-delivery-20261116/README.md)通过：18项STOPPED检查，
216.388秒、0新增GPU。默认430e7985/Guardian e8290fa1，人工保留根
7230ac61具备outer及原周期迁移；自动payload c789291b保持。原核心/
客户端/外层/profile状态不清空；继续实际合main、冷/资源及全部master。
以下按原身份保留阶段历史，旧“待发行”不代表当前状态。

R09[发布前可恢复维护](evidence/serve-root-delivery-preflight-20261116/README.md)完成：
退休发布副本32→30、版本登记15→13，均移入归档，0删除/0GPU，
原策略/预算/default保持。继续现代保留根与候选的STOPPED发布、
原周期回退/前进及运行入口检查；实际默认仍rev174。


R09[分项真实业务已补齐](evidence/serve-root-rollback-business-20261116/README.md)：
原消费周期/工具续答及原周期再前进、新保留根真实outer故障恢复、
新候选健康均通过。累计9启动/18Chat含自检/1探测、938.526秒，
871次NVML正常，四段清理通过。三个原FAILED保留，不称原连续链
通过；430e7985/7230ac61待实际默认发布，rev174保持，master继续。


R09[原周期前进及新保留根outer恢复通过](evidence/serve-root-remaining-partial-20261116/README.md)：
216.573秒内先两项已完成；新候选0启动/0Chat时组内截止触发，
全部清理通过，原组仍FAILED。仅补原计划候选1启动/2Chat/150秒，
累计上限仍9/18/1探测、GPU≤1040秒；默认rev174保持。


R09第二段[短验及控制重置修复](evidence/serve-root-rollback-control-reset-20261116/README.md)：
原状态恢复JSON通过，累计5启动/10Chat/1探测；自有Guardian故障时
只读status连接重置被驱动误判终止，已补4项CPU并修复。两段清理
全部通过，原失败保留。[剩余事实短验](evidence/serve-root-remaining-plan-20261116/README.md)
冻结4启动/8Chat含自检/225秒：原周期前进、新保留根outer恢复及新
候选健康，累计9/18/1探测、GPU<900秒；默认rev174保持。


R09首次短验后的[修复CPU通过](evidence/serve-recovery-wait-cpu-20261116/README.md)：
三个角色的真实预算等待投影及原生指标、首次/已建立整根迁移合同
的保留。10项CPU通过，首次迁移审查后只重验3项；独立Guardian
零警告。原预算不变，[补验驱动2项CPU](evidence/serve-root-rollback-completion-driver-20261116/README.md)
通过；等待原窗口实际到期，仅补3启动/6Chat含自检/300秒，累计
7启动/14Chat/1探测、GPU租约总时≤900秒。
[修复后新双包CPU](evidence/serve-recovery-wait-sealed-20261116/README.md)2项通过，
430e7985/7230ac61业务/发行待完成，默认rev174保持。


[R09整根回退修复](SERVE_ROOT_ROLLBACK_2026-11-16.md)继续实现：
8项纯策略CPU已闭合（原启动判定用例错误保留，实际入口修正后通过）。
已接有界检查点/原STOPPED锁内迁移、实际目标payload的profile、
outer保留与v19封包；检查点与退休登记一起移动。[24项针对性CPU](evidence/serve-root-rollback-cpu-20261116/README.md)首轮
全部通过，55.819秒，0新增GPU，rev174保持；真实双根及[4项驱动CPU](evidence/serve-root-rollback-driver-20261116/README.md)
通过，实际业务执行前修订为6启动/12Chat含自检/1探测/900秒，
覆盖真实周期消费、outer及再前进。[首次业务](evidence/serve-root-rollback-business-failure-20261116/README.md)
4启动/8Chat/1探测后在outer新Guardian启动处因原gateway预算冷却中断；
原周期恢复及工具续答已过，诊断错误显示STARTING/0等待须修复。
STOPPED/进程/锁清理成功，默认保持，完整恢复/再前进/发行仍待完成。

[rev174缓存交付完成](evidence/serve-chat-cache-delivery-20261116/README.md)：
78d6bd7b/0851dab3进入实际默认，17项STOPPED检查与整根回退再前进
通过，0新增GPU；状态预算和未改控制保持。登记池满问题已实质修复。
继续R09人工回退能力/周期迁移、PR实际合main及其余全部master。

[版本登记留存已修复并应用](evidence/serve-version-retention-20261116/README.md)：
8项CPU通过，首次setup夹具错误保留；实际只归档2个退休周期，16→14，
原策略/预算/default全保持，0删除/0GPU。CI登记直接用例，job时限按
实测成本调至40分钟。继续已验缓存包的STOPPED发行及其余全部master。

[版本周期留存修复](SERVE_VERSION_RETENTION_2026-11-16.md)正在开发：
rev166已有16个登记，发布前发现容量满；尚未切换默认。补最多4文件的
显式可恢复归档，保护当前/保留根/pending/活动引用和原预算，冻结8项CPU、
0新增GPU；通过后最多归档2个退休登记，再继续已验缓存包交付与全部master。

[Chat缓存精确包版本短验通过](evidence/serve-chat-cache-version-business-20261116/README.md)：
保留c789291b为1启动/2生成，正式78d6bd7b为2启动/4生成/1探测；
严格租约46.978/100.362秒，45/94项NVML正常。故障后实际视频/历史SSE、
缓存及既有能力保持，清理通过。继续STOPPED可回退发行，rev166保持；
全部master继续，人工旧根outer与版本周期问题须继续修复。

[Chat标准缓存候选主轻资格/封包通过](evidence/serve-chat-cache-profile-business-20261116/README.md)：
2启动/10请求/86.044秒，82项NVML正常；两侧实际schema/工具/SHA续答/
embedding/视频通过。正式78d6bd7b、保留控制c789291b及两项sealed CPU
通过；控制和四个原生程序字节保持。继续1启动/2生成及2启动/4生成/
1探测的精确包版本验收，rev166未改，全部master继续。

[Chat标准缓存实际业务接受](evidence/serve-chat-cache-business-20261116/README.md)：
0851dab3累计2启动/8项，前五原捕获复核、后三短验通过。MTP/target
实际SSD恢复、JSON/SSE、client/key隔离、显式禁缓存和默认隐式写入
闭合；无重复生成。首次驱动失败保留，聚合指标覆盖7项而非8项。
模型/端口/锁清理，rev166保持。继续精确主轻/封包恢复及全部master。

[Chat缓存首次业务驱动中断](evidence/serve-chat-cache-business-failure-20261116/README.md)：
1启动/5生成/48.076秒，46项NVML正常。实际双断点SSD写入、2320 token
SSE恢复、client/key隔离已过；显式禁缓存的可选usage字段省略导致
脚本中断。修正判定并以原捕获核对前五项，终态成功5，最终指标仅到4。
无引擎改动、无重复生成；只补余下3项，额外1启动/3生成/600秒，
累计2启动/8项，rev166保持，全部master继续。

[Chat缓存执行CPU通过](evidence/serve-chat-cache-cpu-20261116/README.md)：
20项C++、4项schema、3项判定器正负控、20项native/schema、715来源/
7负控通过；零警告。独立导入失败已修复并保留。候选未替换rev166，
继续1启动/8生成/900秒真实SSD/JSON/SSE/客户端及key隔离短验；
PR32最终head检查与全部master继续。

[Chat标准缓存下一组实现](SERVE_CHAT_CACHE_2026-11-16.md)已冻结：独立mode/
ttl合同、内容断点进入实际模板/token边界、默认隐式及最近80查找，
复用SSD隔离/故障保护；Responses原合同保留，媒体指纹仍必做。
先最多20项针对性C++/24项native-schema，随后最多1启动/8生成/900秒；
rev166保持，PR32检查中，全部master继续。

[rev166可回退交付完成](evidence/serve-input-attribution-delivery-20261116/README.md)：
17项检查通过；默认0bbfd5e6/7a23cb30，Key/配额/容量与历史预算保持。
控制/客户端/原生辅助程序没有改动，0新增GPU。继续PR32实际合main、
其余五个Chat字段、媒体缓存/后端、客户端/资源/冷启动和全部master；
以下阶段记录按其原身份保留，过去的“待发行”不代表当前默认状态。

[输入归属精确包版本短验通过](evidence/serve-input-attribution-version-business-20261116/README.md)：
保留98b4b47e为1启动/2生成，正式0bbfd5e6为2启动/4生成/1探测；
严格租约46.915/101.523秒，45/95项NVML正常。故障回退后实际视频/
历史SSE、新归属/档位/default及媒体/MCP/embedding能力保留；自有进程/
端口/锁清理通过。首次finalize参数错误与修正保留，0重复GPU。
rev158保持，继续17项STOPPED可回退发布、PR与全部master。

[输入归属候选主轻资格/封包通过](evidence/serve-input-attribution-profile-business-20261116/README.md)：
2启动/10请求/88.426秒，83项NVML正常；两侧实际schema/工具/SHA续答/
embedding/视频通过。正式0bbfd5e6、保留控制98b4b47e及两项sealed CPU
通过；控制和四个原生程序字节保持。继续1启动/2生成及2启动/4生成/
1探测的精确包版本验收，rev158未改，全部master继续。

[输入归属业务接受](evidence/serve-input-attribution-business-20261116/README.md)：
7a23cb30累计2启动/7生成，六项不同业务覆盖Chat JSON/SSE/实际档位/
归属、文档工具/SHA、Responses省略/null medium与存储。两次驱动
投影/SDK便利字段失败保留，修正后用实际wire和CPU正负控闭合，0追加
GPU。第二项剩余对象用生产原生状态API在CPU删除并确认404；不称
已重跑HTTP DELETE。模型/端口/锁清理，rev158保持，继续主轻与可回退
发行、其余五个Chat字段及全部master。

[输入归属短验首轮未通过](evidence/serve-input-attribution-business-failure-20261116/README.md)：
1启动/5生成，四条Chat与实际文档SHA闭环通过；Responses GET默认
投影logprobs导致驱动全量相等断言失败。唯一差异已核对，核心5/5
成功、清理通过；补显式include，冻结仅1启动/2生成/600秒受影响
Responses短验。引擎不改、四条Chat复用、rev158保持，master继续。

[标准输入归属CPU通过](evidence/serve-input-attribution-cpu-20261116/README.md)：
18项C++、5项schema/客户端/驱动、16项原生/公开合同对照、715来源/
7负控及生成物一致性通过，零警告。候选7a23cb30补Chat归属摘要和
实际档位、Responses新medium默认及旧queued语义/归属保持。
旧README类型图错误已修正；rev158未改，继续最多1启动/6生成/
900秒实际业务。PR31已实际合main，全部master继续。

[下一组标准输入合同](SERVE_INPUT_ATTRIBUTION_2026-11-16.md)已冻结：Chat
safety_identifier归属摘要和实际service_tier返回、Responses新请求medium
默认及旧queued快照语义保持。先成组开发/针对性CPU，再最多1启动/
6生成/900秒实际业务；rev158保持，PR31最终head CI中，全部master继续。

[rev158可回退交付完成](evidence/serve-chat-style-delivery-20261116/README.md)：
17项检查通过，默认cba9f596/e0b34b43，停止意图/历史预算/Key保持。
先前真实工具失败、迁移拒绝和两处路径调用缺陷均保留原记录；
修复后按受影响项验收。继续PR31及标准输入/媒体缓存/后端/资源等
全部master任务；本组完成不代表完整兼容或总目标结束。
以下为按原身份保留的阶段记录，“待发行”不代表当前默认状态。

[Chat 迁移修复精确包短验通过](evidence/serve-chat-style-migration-business-20261116/README.md)：
正式cba9f596实际2启动/4生成/1探测/102.376秒，NVML 96项正常。
一项控制源修复后版本回退/冻结视频SSE/能力与历史保持；同一引擎、
主轻资格和保留1b90a1bf按原身份复用。清理通过，继续17项STOPPED
可回退发布及PR31，rev150未改，全部master继续。

[Chat 能力迁移修复CPU通过](evidence/serve-chat-style-migration-cpu-20261116/README.md)：
发布准备发现旧规则拒绝effort枚举扩展，默认未改。限定已审查枚举集合，
保留自动等价/禁止丢能力；4项CPU、真实封包资格/2负控、715来源/7负控
通过。重封正式包一项控制源，复用原引擎/主轻/保留包；冻结2启动/
4生成/1探测/600秒版本短验。rev150保持，全部master继续。

[Chat 候选精确包/版本恢复通过](evidence/serve-chat-style-version-business-20261116/README.md)：
保留1b90a1bf独立1启动/2生成，正式d8960eed实际2启动/4生成/1探测；
严格租约48.066/103.580秒，NVML 46/97项正常。
回退后冻结视频SSE/能力/历史保持，持久STOPPED与资源清理通过。
保留包首个finalize因argv路径字符串不一致拒绝，原始记录保留；同解释器/
驱动/计划的补充核对及1正向5负控通过，0追加GPU。rev150保持，继续
17项STOPPED可回退发布、PR31及全部master。

[Chat 候选主轻资格/封包通过](evidence/serve-chat-style-profile-business-20261116/README.md)：
2启动/10请求/92.497秒，88项NVML正常；两侧实际schema/工具/SHA续答/
embedding/视频通过。正式d8960eed、保留控制1b90a1bf及两项sealed CPU
通过；控制和四个原生程序字节保持。继续1启动/2生成及2启动/4生成/
1探测的精确包版本验收，rev150未改，全部master继续。

[Chat 样式实际业务通过](evidence/serve-chat-style-business-20261116/README.md)：
e0b34b43同一严格租约1启动/6Chat/54.202秒，52项NVML正常。
原问题/判定保持；默认/null、padding双向、双choice概率/usage、一次
实际文档工具/结构化续答及同PID普通Chat通过，自有资源清理。原
none语法失步失败保留；继续新引擎主轻资格与可回退交付，rev150未改。

[none模板/约束修复CPU通过](evidence/serve-chat-style-grammar-cpu-20261116/README.md)：
8项直接C++含真实grammar负控，来源/生成物检查通过，最终零警告。
仅共享样式、模板、约束三项运行时变化；初次key类型构建失败保留。
候选`e0b34b43`继续原六条业务/原判定，最多1启动/6Chat/900秒。
rev150未改，全部master继续。

[Chat 样式实际业务首轮未通过](evidence/serve-chat-style-business-failure-20261116/README.md)：
1启动/4Chat/45.196秒，前三条通过，第4条工具无成功终态、0工具执行。
根因是none模板关闭thinking、语法仍按enable_thinking缺省开启，第二次
调用未被解码约束屏蔽。现统一解析，正在做真实grammar/模板针对性CPU；
原失败保留，rev150未改，自有GPU资源已清理，全部master继续。

[Chat 样式/流式针对性CPU通过](evidence/serve-chat-style-cpu-20261116/README.md)：
15项不同C++、6项schema/客户端/驱动、32项公开/原生解析对照通过；
715来源/7负控及生成物一致性通过，主机/引擎/probe零警告。首轮夹具
误读prompt_end，按既有合同修正后单项复验，14项成功复用。候选
`8b4e1fd1`待最多1启动/6Chat/900秒业务，rev150未改；全部master继续。

[Chat 样式与流式合同](SERVE_CHAT_CONTRACT_PARITY_2026-11-16.md)候选已实现：
完整 effort/null/medium 缺省、verbosity 实际提示、默认 SSE obfuscation
及关闭选项；原生来源坐标保持，公开生成器/客户端同步。15项C++及
4项schema/客户端检查范围已冻结，尚未测试/发行；rev150保持。
PR30最终head两组CI通过并实际合main，下一步定向CPU和最多1启动/
6 Chat/900秒业务短验，全部master继续。

[当前协议/总任务再审计](SERVE_SURFACE_REAUDIT_2026-11-16.md)已逐条核对
45个Chat字段、21个Completions字段、74后端/123原生路由；修正旧描述
中的已交付能力，仍缺8个冻结标准Chat字段、流式obfuscation、完整
reasoning effort/历史及媒体缓存等合同。33项仍required；下一组实际
补Chat入口差异。714来源锚点/7项负控、新增26项引用和默认身份核对通过，0GPU/运行时
未改；master继续。

[rev150显示几何可回退发行完成](evidence/serve-video-geometry-delivery-20261116/README.md)：
17项检查、实际整根回退再前进通过，最终STOPPED；预算/历史/Key保持，
0新增GPU。归档首次路径错误发生于写入前，改为读取真实generation
回执，失败记录保留。继续PR30合main及全部兼容/资源/后端任务。
以下是按原身份保留的阶段记录，其中“待发行”不代表当前默认状态。

[显示候选精确包/版本回退通过](evidence/serve-video-geometry-version-business-20261116/README.md)：
保留a9bd9c73独立1启动/2生成，正式8d9c07f1实际2启动/4生成/1探测；
严格租约53.503/101.308秒、51/95项NVML正常。冻结镜像视频SSE角点/
布局、能力/配置历史保留，持久STOPPED和清理通过。rev142保持，
继续可回退发布与PR30实际合main；未改控制/数值证据复用、master继续。

[显示候选主轻资格与封包通过](evidence/serve-video-geometry-profile-business-20261116/README.md)：
a7bd6785两侧实际schema/文档工具/embedding/视频，2启动/10请求/
87.791秒、83项NVML正常，注册表a4c27749；控制源码与四个原生程序
字节保持。正式8d9c07f1与保留a9bd9c73包两项隔离CPU通过，继续
1启动/2生成和2启动/4生成/1探测验证新包及冻结镜像SSE版本回退。
rev142保持，全部master继续。

[显示几何实际业务通过](evidence/serve-video-geometry-business-20261116/README.md)：
1启动/4创建/52.610秒，51项NVML正常。原始上传/inline视频的旋转、
镜像SSE、像素比例组合四角与布局正确，计数/同PID普通文本/清理均过。
保留原失败，问题与预期未放宽；宽高来源元数据与像素修正分别记录。
a7bd6785待新主轻配置资格与可回退发行，rev142保持，master继续。

[显示几何实际短验](evidence/serve-video-geometry-business-failure-20261116/README.md)
在镜像布局回答处未通过：旋转正确、镜像四角正确却回答square。
1启动/2创建/43.682秒、43项NVML正常，自有资源已清理，rev142保持。
实际冻结PNG和最终ProcessVideo网格均为128×64，未发现方形化；
[共享来源尺寸5项CPU已过](evidence/serve-video-display-source-cpu-20261116/README.md)，
仅将已验证宽、高补入输入说明，不更改像素或给定layout/颜色答案。
候选a7bd6785继续相同原问题/预期最多1启动/4创建/600秒补验。

[视频显示方向/比例修复](SERVE_VIDEO_GEOMETRY_2026-11-16.md)
[针对性CPU已过](evidence/serve-video-geometry-cpu-20261116/README.md)：
14项不同C++、2项驱动、714来源锚点/7项负控；主机/引擎零警告。
首轮两项失败已保存并修复：实际系统库遗漏反射方向，原生有界MP4
矩阵解析补齐；EOS保留原容量错误。8项受影响复验通过，6项未变成功
记录复用。候选6135c33e继续最多1启动/4创建/600秒实际显示业务；
尚未发行，rev142保持、master active。

[PR29旧媒体/MTP断言已修复](evidence/serve-media-client-ci-20261116/README.md)：
原f8f25148两组CI仅同一旧断言失败，本机针对性1项首过、零警告；
运行时与rev142不变、0GPU。新head两组远端检查成功且已实际合main，
master继续。

[rev142托管视觉可回退发行完成](evidence/serve-managed-vision-delivery-20261116/README.md)：
17项STOPPED检查通过，实际回退再前进保持配额/历史/预算，默认
启用媒体且文本保留MTP。0新增GPU；继续PR29合main及全部master。

[精确媒体包与版本回退通过](evidence/serve-media-version-business-20261116/README.md)：
保留包73b45ed3独立1启动/2生成，正式包7a8e9cd2实际2启动/4生成/
1探测完成整树回退，视频历史/SSE和能力保留，自有资源清理。
原默认rev134保持，继续可回退发行及全部master。

[媒体版本回退驱动4项CPU首过](evidence/serve-media-version-driver-cpu-20261116/README.md)：
真实封包隔离导入、媒体启动、判定和预算通过。新派生保留控制包
73b45ed3待1启动/2生成/300秒真实资格，再对正式7a8e9cd2完成
2启动/4生成/1设备探测/600秒整树回退。rev134不变，全部master继续。

[视觉服务实际自动降级/恢复通过](evidence/serve-media-managed-business-20261116/README.md)：
3启动/12请求/205.133秒，192次NVML正常；真实RAM压力触发主→轻→主，
8.125GiB自有页全释放，视频冻结历史/一次工具执行/配额连续/晋升SSE
和普通Chat通过，控制树保持。实际受控包8fe97f50已验；正式候选
7a8e9cd2及版本回退仍须完成，rev134不变，全部master继续。

[托管视觉驱动首次失败已定位](evidence/serve-media-managed-driver-failure-20261116/README.md)：
1启动/1自检后，驱动间接导入生产包未携带的TLS夹具失败；尚无媒体
请求或RAM压力，持久STOPPED与自有资源清理完成。纯wire检查替代
测试夹具导入，实际sealed namespace判定CPU单项通过；引擎/包保持，
继续最多3启动/12请求/900秒原恢复场景，rev134保持、master继续。

[托管视觉恢复驱动/封包CPU首过](evidence/serve-media-managed-driver-cpu-20261116/README.md)：
5项CPU通过，四个原生服务程序零警告且字节与rev134相同。候选包
7a8e9cd2待实际恢复；继续最多3启动/12请求/900秒真实内存压力下的
主→轻→主、视觉冻结历史/工具/配额与SSE，rev134保持，全部master继续。

[媒体主轻真实资格通过](evidence/serve-media-profile-business-20261116/README.md)：
2启动/10请求/96.528秒，92次NVML正常；两侧均完成工具实际SHA闭环、
结构化输出、embedding和原VP8首尾识别。仅MTP开关不同，能力相等，
准入估算103887276952/93982841120字节。自有资源清理、rev134保持；
继续同能力托管恢复/发行及全部master，尚未认作自动恢复通过。

[混合文本/视觉实际业务通过](evidence/serve-managed-vision-business-20261116/README.md)：
1启动/4创建/68.710秒，66次NVML正常。前后文本各10步MTP，中间真实
视频和PDF普通解码且答案正确，同PID/计数/清理闭合，rev134保持。
[媒体资格/迁移38项CPU首过](evidence/serve-media-profile-contract-cpu-20261116/README.md)，
继续同cd640ccc最多2启动/10请求/900秒主轻真实媒体资格，再实际恢复
与发行；全部master仍active。

[托管视觉配置CPU首过](evidence/serve-managed-vision-cpu-20261116/README.md)：
9项C++/25项Python/7项来源负控通过，主机与引擎零警告。共同加载、
按请求媒体普通decode、版本2容量和独立观测、主轻媒体资格合同已实现。
候选cd640ccc尚待实际混合业务与恢复；最多1启动/4创建/600秒短验，
rev134保持，全部master继续。

[原生视频实际业务通过](evidence/serve-video-business-20261116/README.md)：
1启动/8创建/53.447秒，52次NVML正常；MP4计数、VP8 SSE、实际函数
VP9续答、MCP真实读取、删除后的冻结回放、损坏后同PID普通推理均过。
历史失败保留，自有进程/端口/锁清理，rev134未改。随后同候选的
[受影响入口短验](evidence/serve-prepared-source-business-20261116/README.md)
也通过：1启动/4创建/65.750秒、63次NVML正常；Chat PDF/WebP流式、
Responses真实项目文档与计数、同PID普通Chat闭合。继续
[托管视觉配置与恢复](SERVE_MANAGED_VISION_2026-11-16.md)，rev134保持，
全部master仍active。

[统一投影原业务](evidence/serve-video-projection-business-failure-20261116/README.md)
在VP8仍拒答：实际看见红/蓝，却将平均视觉组标签当作缺首尾帧的证据。
当前共用同一时间戳函数，并将每组标签对应到实际源帧的显示区间；
不改像素、采样、张量或均值。[7项CPU首过](evidence/serve-video-group-timing-cpu-20261116/README.md)，
主机/引擎零警告；随后最多1启动/8创建/
900秒原业务；失败未验收，rev134保持，全部master继续。

[通用说明业务](evidence/serve-prepared-input-trust-business-failure-20261116/README.md)
仍在VP8拒答，未验收；[相同输入JSON/SSE诊断](evidence/serve-video-transport-diagnostic-20261116/README.md)
结果完全相同，未观察到传输差异。当前修正纯视频的空文档包装，
共用模板层的私有数据说明接通Chat/Responses、计数、预检和Batch，
不插入额外历史消息。[16项CPU最终通过](evidence/serve-video-projection-cpu-20261116/README.md)，
主机/引擎零警告；继续最多1启动/8创建/900秒原业务；
原问题、refusal协议和答案判定保持，rev134不变，总目标继续。

[帧时长业务](evidence/serve-native-video-intervals-business-failure-20261116/README.md)
仍拒答，未验收；随后[三组固定诊断](evidence/serve-native-video-diagnostic-20261116/README.md)
均答对，指向“来源可信度/指令权威”语义与结构化输出的组合问题。
当前原生适配为真实准备文件/MCP结果加入通用数据说明，保留refusal
协议、原问题/答案、权限和像素；普通文本不添加。[5项不同CPU最终过](evidence/serve-prepared-input-trust-cpu-20261116/README.md)，
首轮MCP映射类型错误保留并针对性修复；主机/引擎零警告。继续
最多1启动/8创建/900秒原业务，默认rev134保持，总目标继续。

[视频首尾说明补验](evidence/serve-native-video-followup-failure-20261116/README.md)
仍失败：模型已识别末帧蓝色，但把5.5秒PTS误解为6秒结束前缺帧。
1启动/4创建后按失败清理，rev134不变；继续补齐实际帧显示时长、
worker传递、快照完整性和半开时间区间表达，不根据容器时长虚构帧。
[20项C++/7项来源负控已过](evidence/serve-native-video-intervals-cpu-20261116/README.md)，
主机/完整引擎零警告；之后最多1启动/8创建/900秒验证原问题，业务仍未验收。

[首次视频业务短验](evidence/serve-native-video-business-failure-20261116/README.md)
在1启动/4创建后失败：直接MP4、VP8 SSE和计数通过，工具VP9续答拒答。
原始首尾帧与media执行存在，45项NVML均OK、自有资源清理完成、rev134
保持。当前补充统一投影对实际视觉内容/首尾/采样遗漏的准确说明，
明确报告refusal；冻结4项C++/2项Python及最多1启动/8创建/900秒补验，
原问题与判定不变。[4项C++/2项Python和来源检查已过](evidence/serve-native-video-followup-cpu-20261116/README.md)，
主机/引擎零警告；仍须实际业务证明修复，不将失败转成兼容完成。

[视频业务驱动CPU准备](evidence/serve-native-video-business-driver-cpu-20261116/README.md)
已过：2项C++、5项Python实际TLS/请求/判定器、7项来源负控/712锚点，
主机/引擎零警告；能力发现区分编译、媒体配置与运行库检查。
候选e5e67af8已冻结，继续最多1启动/8创建/900秒SDK/模型
业务短验；本记录时0GPU、rev134保持，不把CPU通过当实际业务通过。

[统一视频输入CPU验证](evidence/serve-native-video-input-cpu-20261116/README.md)
已完成：38项不同C++（39次执行）、1项实际CLI封包、7项来源负控/712
锚点最终通过，主机/完整引擎零警告。Files、MCP、计数、工具角色、
审核和v3快照接线；跨文件/调用零余额阻止新解码。首轮计数夹具失败
保留并单项修复复验；0GPU、rev134保持。候选8dbab79f尚待实际业务/
恢复发行；PR29前驱2276928f两组远端CI均成功，不代表本批新代码已验。
继续SDK/模型业务、格式组合和全部master。

[原生视频worker](evidence/serve-native-video-worker-cpu-20261116/README.md)
已通过13项不同C++（26次执行）、7项来源负控/712锚点，最终主机/
完整引擎零警告；固定软件H.264/VP8/VP9真实帧与PTS、隔离/配额有证据。
首轮截断容器超时已修复，失败保留；0GPU、rev134保持。PR29旧head
一组CI的PID夹具竞争也已修复，1项本机CPU及前驱两组远端CI均过。
继续[统一视频文件输入](SERVE_NATIVE_VIDEO_INPUT_2026-11-16.md)的快照、
时序投影和共享预算，不能把worker通过当完整API或云账户业务通过。

Graph分页兼容已接线保留原URL的受保护续接、等价编码/OData路径及
认证提供方返回的用户/日历别名；[47项CPU首过](evidence/serve-mcp-graph-cursor-cpu-20261116/README.md)。
原工具/参数/授权与一小时期限绑定，复用现有密码组件，无新依赖。
来源7项/706锚点过、构建零警告，0GPU、rev134保持，真实账户仍待验。
继续MCP下载视频进入实际媒体准备，不能把MP4普通文件投影当完整业务。

Google Vids长耗时下载已接线一次发起、显式pending和原任务单次续接；
[30项CPU首过](evidence/serve-mcp-drive-lro-cpu-20261116/README.md)，7项来源
负控/705锚点过，主机和完整引擎零警告。认证下载URI留在worker内，
账户令牌只发固定Google API主机；无重试或模型重启，0GPU。
候选工作已建立[PR29](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/29)，
仍为draft，真实账户/业务/发行未完成；rev134保持，全部master继续。

Dropbox六工具候选已完成[25项CPU和1项实际CLI八提供方封包首过](evidence/serve-mcp-dropbox-cpu-20261116/README.md)，
主机/完整引擎零警告，来源7项负控/704锚点过，0GPU、rev134不变。
八提供方42工具均有原生候选实现；真实账户、特殊格式、业务组合及
可回退发行仍未完成。继续Google Vids异步下载与全部master。

Microsoft Graph四提供方20工具已实现候选，[40项不同C++、1项实际CLI
最终通过](evidence/serve-mcp-graph-cpu-20261116/README.md)，7项来源负控、
703锚点通过，最终构建零警告。OAuth与文件下载能力隔离，邮件列表
游标不能扩大到正文；原始失败及针对性补验保留。0GPU、rev134保持，
七提供方候选仍待真实账户/发行；继续Dropbox、特殊格式及全部master。

Drive五项、Gmail六项原生只读工具已进入候选：实际下载/Office导出、
分页与MIME正文/可选附件接线。[36项C++、1项实际CLI封包首过](evidence/serve-mcp-google-cpu-20261116/README.md)，
最终构建零警告、7项来源负控通过，0GPU。不新增链接密码库，系统
字符转换仅在隔离worker内使用。三个提供方仍待真实账户业务/发行，
继续Graph/Dropbox及特殊格式，不将本机夹具称为云账户实际验收。

[原生提供方适配](SERVE_MCP_PROVIDERS_2026-11-16.md)的Calendar五项
只读工具、固定主机、OAuth权限目录、时区/游标及错误处理已实现；
[28项C++、14项Python/实际CLI最终通过](evidence/serve-mcp-provider-calendar-cpu-20261116/README.md)，
主机/完整引擎零警告、7项来源负控通过，0GPU。首轮小数秒与恢复
夹具失败保留并补验。实际账户业务和可用发行仍待完成；继续其余
七提供方及全部master，八提供方42工具没有缩减。

[隧道agent配置一致性修复](evidence/serve-mcp-agent-config-cpu-20261116/README.md)
已完成：11项不同C++、5项实际CLI、7项来源负控最终通过；显式非零
重定向不再静默覆盖，check使用实际worker序列化预算，避免检查成功
而启动失败。首轮夹具/来源门禁失败保留，针对性补验；0GPU。候选
9cf9af38独立保留、尚待发行；rev134默认不变。PR28此前交付已合入，
本修复随提供方原生适配继续交付，全部master任务保持active。

以下保留各阶段发生时的默认身份；当前已交付身份以上表为准。

[MCP能力包的受控恢复](evidence/serve-mcp-managed-20261116/README.md)
已通过：回退包1启动2业务，整树切换2启动4业务1探测；Guardian保留、
控制器/模型/入口换代，原程序/调用/schema/存储与预算保持，实际资料
只读1次并SSE续答。MCP远端业务与会话CPU证据分别绑定，未混称为
同一端到端故障场景。累计6启动22请求（含2embedding）1探测，全部
清理；主包437e477c、回退52c399d7待STOPPED发行，rev126仍保持。

[MCP恢复合同与主轻资格](evidence/serve-mcp-recovery-contract-20261116/README.md)
已通过：同一引擎主/轻2启动8业务、88.606秒；随后修复MCP能力迁移
和自动回退私有策略保持，7项CPU首过、来源负控最终通过。4个helper
零警告构建，3个字节不变；引擎和编译来源不变，业务资格明确复用。
当前累计3启动/16业务；继续封包与受控恢复短验，rev126保持。

[MCP真实模型与SDK业务](evidence/serve-mcp-business-20261116/README.md)
已通过：1启动/8创建/89.225秒，直接HTTPS审批/拒绝、原生隧道SSE、
实际资料与资源读取、未知结果不重放及同PID普通推理；84次NVML正常。
本机进程/端口/锁已清理，现代持久会话按续接合同保留。首次入口缺少
继承租约参数在0启动/0请求时失败，原件保留；随机凭据和私钥不公开。
默认rev126保持，继续精确主轻资格、恢复发行/main及全部33项master。
[驱动CPU证据](evidence/serve-mcp-business-driver-cpu-20261116/README.md)复用。

- [隧道公开合同与可选前置22项CPU最终通过](evidence/serve-mcp-tunnel-contract-cpu-20261116/README.md)：
  三条OpenAPI/schema、两种可选nginx的TLS/受保护头与独立准入已接通；
  修复存储Chat文件消息引用缺口。首次失败保留，来源7项过，复用未变
  C++构建；0GPU、rev126保持。继续SDK/真实业务、提供方、恢复发行及全部master。

- [自有agent 32项CPU及4项实际CLI最终通过](evidence/serve-mcp-tunnel-agent-cpu-20261116/README.md)：
  固定私网HTTP/SSE、真实broker往返、TLS入口/资料、会话清理及
  35秒监护/重启预算已验证。鉴权终态与unknown传递缺口已修复；
  实际CUDA映射超过512MiB，MCP/agent改为既有1GiB虚拟上限。
  首次失败保留，最终主机/引擎零警告、来源7项过；0GPU、rev126
  保持。继续提供方/SDK/业务/恢复发行/main，完整隧道与master未完成。


- [原生隧道隔离传输38项CPU最终通过](evidence/serve-mcp-tunnel-transport-cpu-20261116/README.md)：
  原权限/加密会话/实例亲和、审批/资源/程序调用、已知终态和清理
  已接线；头转换和会话schema遗漏已修复，首次失败保留。来源7项、
  主机/完整引擎零警告，0GPU、rev126保持；继续自有agent真实HTTP/
  SSE、SDK/业务及恢复发行，完整隧道与全部master尚未完成。

- [原生隧道接入26项CPU、5项入口/封包首过](evidence/serve-mcp-tunnel-api-cpu-20261116/README.md)：
  native配置/私有绑定、受保护头、真实TLS和独立poll/response容量
  已验证；来源7项及主机/完整引擎/ingress零警告，0GPU、rev126保持。
  继续隔离MCP传输与agent接线及完整交付，不能把入口当完整隧道。

- [原生隧道broker 16项CPU通过](evidence/serve-mcp-tunnel-broker-cpu-20261116/README.md)：
  单次取走、精确实例/token/作用域、未知不重放、进度/终态与容量
  已验证；7项来源负控、主机/完整引擎零警告通过。映射远端CI成功，
  0GPU、rev126保持；继续HTTP/实际MCP/私网agent接线与完整交付。

- [MCP部署映射29项CPU最终通过](evidence/serve-mcp-mapping-cpu-20261116/README.md)：
  独立网络/作用域、原审批/资源目标、离线冻结及程序/Batch实际HTTP
  已验证；执行策略快照遗漏已修复，首次3项失败保留。8项原生
  策略/封包、7项来源负控及完整引擎零警告通过，0GPU、rev126保持。
  继续原生隧道/提供方适配及完整业务交付，不能把映射当完整隧道。

- [MCP会话实际执行41项CPU最终通过](evidence/serve-mcp-sessions-execution-cpu-20261116/README.md)：
  加密目录、原调用/资源绑定、审批/冻结重启、凭据更新与资源失能
  持久化已验证；首次2项夹具失败保留并修复。绝对期限阻止慢发布/
  许可后派发；7项来源负控及完整引擎零警告，传输远端CI通过。
  0GPU、rev126保持；继续映射、SDK/真实业务/恢复发行及全部master。

- [原生会话传输30项CPU首过](evidence/serve-mcp-sessions-transport-cpu-20261116/README.md)：
  有状态列表/调用/资源、现代恢复、旧SSE连续、隔离与过期通过；
  7项来源负控和完整引擎零警告，0GPU、rev126保持。计数远端CI
  的四项事务恢复失败已定位并针对性补验通过，失败保留；继续
  加密目录/调用接线及完整MCP与全部master交付。

- [计数统一26项CPU首过](evidence/serve-mcp-count-cpu-20261116/README.md)：
  MCP图像与生成输入一致、多份CSV精确额度及冻结复用、新目录
  只读元数据和全部计数直接合同通过；主机/完整引擎零警告，
  7项来源负控通过。0GPU、rev126保持；继续会话/映射与全部交付。

- [MCP实际媒体执行20项CPU最终通过](evidence/serve-mcp-media-execution-cpu-20261116/README.md)：
  实际HTTP图像/文档/资源、冻结续接、共享预算、晚到sheet原配额
  和历史资源清理责任已接线；首次媒体夹具配置失败保留，补单项
  通过。主机/完整引擎零警告、7项来源负控通过、0GPU、默认保持。
  继续计数统一、会话/映射与实际模型/恢复发行，master active。

- [MCP共享媒体准备14项CPU首过](evidence/serve-mcp-shared-media-cpu-20261116/README.md)：
  图像/文档进入模型和审核类型，跨调用预算、CSV持久准备/重启
  续接与截断已验证；主机/完整引擎零警告，7项来源负控最终通过。
  0GPU、rev126保持；继续实际执行器/计数及晚到sheet模型入场。

- [MCP模型媒体投影12项CPU首过](evidence/serve-mcp-projection-cpu-20261116/README.md)：
  真实图片/文件准备、持久来源绑定、重启无codec回放及依赖回收
  通过；主机/完整引擎零警告、7项来源负控通过。0GPU，rev126
  保持，762ab1aa远端CI通过；继续实际执行/审核/计数/共享预算及交付。

- [MCP资源冻结16项CPU首过](evidence/serve-mcp-resource-freeze-cpu-20261116/README.md)：
  原调用/目标/权限绑定、单次读取、重启冻结、精确拥有者和共享
  容量已验证；为在途失败预留收尾空间。主机/完整引擎零警告，
  0GPU、默认保持，继续模型媒体准备和全部交付。


- [MCP原生内容与资源读取26项CPU最终通过](evidence/serve-mcp-content-cpu-20261116/README.md)：
  五类内容、资源许可/已知与未知结果、原生错误结构化数据恢复和
  JSON/SSE发布已接线，官方SDK新增1项通过。主机/完整引擎零警告，
  043e5786远端CI全部通过；0GPU、rev126保持。模型媒体准备/资源冻结等继续。


- [程序/MCP媒体边界复核8项CPU首过](evidence/serve-mcp-program-boundary-cpu-20261116/README.md)：
  恢复公开批次的媒体校验边界，文件准备仅映射客户端子调用，避免
  空call_id触及MCP私有结果；完整引擎零警告、0GPU、rev126保持。
  继续MCP媒体/资源、会话/映射及完整业务发行，master保持active。

- [MCP程序子调用49项CPU最终通过](evidence/serve-mcp-program-cpu-20261116/README.md)：
  原生派发、审批续接、混合工具因果顺序、原schema/权限、计数和冻结
  恢复已验证；主机/完整引擎零警告、0GPU、rev126保持。继续媒体、
  会话/映射、完整SDK及真实业务、恢复发行与主线，master保持active。

- [CI正常恢复期限修正](evidence/serve-ci-recovery-deadline-20261116/README.md)：
  277257f6远端4项失败保留；100/250ms只用于故障注入，正常恢复
  保持生产默认期限，4项本机补验通过。继续后续head远端CI确认。

- [MCP存储故障清理续办18项CPU通过](evidence/serve-mcp-cleanup-cpu-20261116/README.md)：
  派发前登记有界元数据，stateless收尾失败继续保留责任；已确认
  进程存活/存储恢复阻塞时普通请求继续，恢复后收尾不重放。主机/
  完整引擎零警告、0GPU、rev126保持；继续程序/媒体/会话/映射及交付。

- [MCP拥有者恢复21项CPU首过](evidence/serve-mcp-recovery-cpu-20261116/README.md)：
  真实进程退出、精确请求清理、未知不重放、已知结果保留和异常续接
  已验证。主机/完整引擎零警告、0GPU、rev126保持；继续存储短暂
  失能后的清理续办及程序/媒体/会话/映射、业务与发行，master active。

- [MCP与Prompt组合11项CPU首过](evidence/serve-mcp-prompt-cpu-20261116/README.md)：
  登记不联网/不授予调用，执行时校验认证目录；当次凭据、冻结版本、
  Batch来源及延迟发现已覆盖。主机零警告、0GPU、rev126保持；继续
  失去拥有者后的调用收尾、程序/媒体等及真实业务发行，总目标active。

- [MCP发现/权限及开放schema23项CPU最终通过](evidence/serve-mcp-discovery-cpu-20261116/README.md)：
  延迟目录实际激活、客户端新定义、权限刷新及故障后续请求恢复；
  修复strict混合工具错误封闭MCP参数，原schema由独立worker校验
  并绑定回执。完整引擎零警告、0GPU、rev126保持，其他组合继续。

- [MCP Batch来源鉴权12项CPU最终通过](evidence/serve-mcp-batch-cpu-20261115/README.md)：
  原始File行与准备身份绑定、重启后计数/真实本地派发、凭据隔离及
  缺凭据不联网已验证；完整引擎零警告构建、0GPU、rev126保持。
  继续目录权限/发现/程序/媒体/恢复等组合及真实业务和发行。

- [向量任务CI夹具期限修正](evidence/serve-ci-vector-deadline-20261115/README.md)：
  正常IO恢复生产默认期限，4处故障注入保留100ms；6项相关CPU首过，
  生产/default未变、0GPU。旧head的4项失败保留，继续最新head检查
  及MCP组合实现，总目标仍active。

- [MCP实际执行拥有者22项CPU首过](evidence/serve-mcp-execution-cpu-20261115/README.md)：
  本地HTTP调用、审批/续接/不重放、冻结目录与凭据隔离、故障后文本
  续答和审核组合已验证；7项来源负控通过、0GPU、rev126保持。
  Batch/程序/媒体/恢复等组合及SDK/真实模型业务继续，尚未发行MCP。

- [MCP请求规划与历史组合19项CPU最终通过](evidence/serve-mcp-planning-cpu-20261115/README.md)：
  工具发现按server_label、原schema/选择、审批/失败历史及截断/压缩
  依赖已接线；首次两项夹具失败保留，修后仅补2项。7项来源负控
  通过、0GPU、rev126保持；继续拥有者凭据/持久准备和实际执行。

- [MCP模型绑定及认证历史13项CPU首过](evidence/serve-mcp-binding-history-cpu-20261115/README.md)：
  原schema、标准可选字段、调用ID续接及原子审批索引已验证；7项
  来源负控通过、零警告，0GPU、rev126保持。继续实际公开执行链
  和全部master，尚未交付MCP接口。

- [MCP输出与派发拥有者16项CPU、2项SDK最终通过](evidence/serve-mcp-output-owner-cpu-20261115/README.md)：
  参数逐片事件、截断/取消形状与并发失败不覆盖在途结果已验证。
  公开执行/历史和组合仍在接线，尚无MCP对外交付；0GPU、rev126
  保持，继续全部master。

- MCP公开输出部件10项CPU及2项官方SDK最终通过；继续接实际
  Responses执行器。并发复核补充派发身份，防重复请求失败覆盖
  在途结果，正在验证直接受影响项；0GPU、rev126保持。

- [MCP目录与持久审批20项针对性CPU最终通过](evidence/serve-mcp-authority-cpu-20261115/README.md)：
  单次派发、未知不重放、原schema及凭据不存储验证通过；首次清理
  夹具失败保留，修后单项通过。0GPU、rev126保持；继续公开MCP
  items/SSE与状态/程序/真实业务，全部master仍active。

- MCP目录/凭据/过滤7项及受影响传输头1项CPU首过；正在接认证
  目录、审批和单次派发的持久状态，尚未开放MCP对外能力。0GPU、
  rev126保持，真实业务与全部master继续。

- PR27精确head `9e0a4b44` 的两组CI全部通过，已[实际合main](evidence/serve-pr27-merged-20261115/README.md)
  （`1f0ed01a`）。程序调用rev126已交付；MCP仍在后续开发中，
  继续审批/公开接线及全部剩余master任务。

- [原生MCP传输部件21项针对性CPU最终通过](evidence/serve-mcp-transport-cpu-20261115/README.md)：
  JSON/SSE/旧传输、单次派发/取消、未回收隔离及HTTPS真实资料读取；
  7项来源负控通过。0GPU、rev126保持；审批/公开接线和业务发行
  继续，不能把传输部件通过当作MCP完整交付。

- [原生MCP实施清单](SERVE_MCP_2026-11-15.md)已固定，正在开发传输、
  审批和实际执行；默认rev126保持。PR27最终head检查进行中，
  全部33项继续，MCP未接线/未验收前不宣称交付。

- 原生程序调用已[可回退发行rev126](evidence/serve-programmatic-delivery-20261115/README.md)：
  17项STOPPED检查全过，实际回退再前进，密钥/配额/core/outer及
  恢复预算保持，0新增模型启动。PR27待最终检查和实际合main；
  剩余协议/工具/模型后端及恢复组合继续，未结束总目标。


- 程序调用受控续答缺陷已[通过真实补验](evidence/serve-programmatic-qualification-20261115/README.md)：
  新引擎主轻2启动8业务、独立回退1启动2业务、整树恢复2启动4业务
  1探测均通过。切换后保留原程序/caller和schema，真实资料只读一次，
  VM恢复后模型成功输出最终SSE答案；自有资源清理。原失败仍保留，
  默认rev118保持，接着17项停止态可回退发布与PR27实际合main；
  剩余全部master任务继续。

- 首次入场前程序续接配额已[修复，26项针对性CPU通过](evidence/serve-programmatic-resume-rate-cpu-20261115/README.md)：
  重新预留未入场提示，保留RPM、输出预算、已发生用量与Batch额度；
  实际引擎d7dd0c6d及主机零警告构建，4个原生helper字节保持。
  默认rev118未改；新引擎资格及受控续接/恢复补验上限5启动14业务
  1探测1800秒，含原失败资格组累计上限10/28/2/3600秒。不跑全量
  性能/质量，业务闭环尚未通过前不发行，全部master继续。

- 程序续接[整树恢复业务未通过](evidence/serve-programmatic-resume-failure-20261115/README.md)：
  控制树和持久调用恢复成功，但受控客户端在首次模型入场前续接程序，
  误用要求已入场的输入追加接口，最终响应503。2启动4业务1探测，
  自有资源已清理、rev118保持。正在修正未入场输入的重新预留，保留
  RPM/输出预算/既有用量，并验证26项直接相关CPU；不关闭配额绕过。

- PR27的无XGrammar构建断言已[修复，两种构建针对性CPU均通过](evidence/serve-programmatic-ci-grammar-20261115/README.md)：
  ON保持regex/Lark实际派发与拒绝，OFF验证零派发及明确错误；生产
  源码/引擎和rev118未变，已有业务及主轻资格复用。继续精确回退包、
  整树恢复、可回退交付及实际合main。另依据PR23/24实际合并回执
  修正N05/R01滞后的任务状态；剩余全部master任务继续。

- 程序调用[真实资料JSON/SSE闭环通过](evidence/serve-programmatic-business-20261113/README.md)：
  修后1启动/8创建/121.786秒，115次NVML正常、资源清理；含首次失败
  累计2启动/9创建/159.939秒。原schema、caller和持久续接保持，实际
  两次读取资料，纯计算/截断/普通业务均通过。PR27已建；继续主轻与
  程序等待期间整树恢复、可回退发行及合main，rev118保持，master继续。

- 大长度语法失败已CPU复现并[修复，11项针对性检查通过](evidence/serve-programmatic-length-repair-20261113/README.md)：
  紧凑计数规则保留min/max、Unicode及JSON转义；不删除约束或放宽资源
  上限。实际引擎零警告构建，补验固定1启动/8创建/900秒，连原失败
  累计上限2启动/9创建/1200秒；业务未重验前不交付，rev118保持。

- 程序调用[首次真实短验失败](evidence/serve-programmatic-business-failure-20261113/README.md)：
  1启动/1创建/38.153秒，首条请求在语法编译阶段返回资源限制，未执行
  程序或客户端工具。自有进程/端口/锁清理，默认rev118保持；正在CPU
  复现大字符串长度约束的编译问题，修复后补原业务，不以失败归档结案。

- 程序调用公开接入[54项不同C++、2项官方SDK及7项Python检查最终通过](evidence/serve-programmatic-integration-cpu-20261113/README.md)：
  授权派发、JSON/SSE、认证续接、媒体冻结/审核/共享预算及异步同步
  组合已验证。媒体临时引用和混合同步等待缺口已修复，失败原件保留。
  实际引擎零警告构建已通过，冻结1启动/8创建/900秒资料闭环，再完成
  适用恢复资格与发行；0GPU、rev118保持，全部master继续。

- 程序调用公开执行链正在接线：program-only工具从直接生成通道移除，
  原生程序输出及caller进入JSON/SSE，持久结果合同从认证快照绑定，
  历史/压缩保留程序依赖；续接进入原共享预算、审核与终结流程。
  这组尚未验证或发行，媒体组合等仍需补齐；rev118保持，全部master继续。

- 程序调用[认证/回放部件19项不同CPU最终通过](evidence/serve-programmatic-authority-cpu-20261113/README.md)：
  跨客户/模型/代码篡改拒绝、调用时参数/结果合同、结果因果回放、
  custom文本/regex/Lark、重启及取消/过期已验证。两项适配失败原件
  保留，仅重验受影响用例；0GPU、rev118保持。继续公开生成与
  JSON/SSE/持久接线、实际业务及全部master，不以部件通过结束总目标。

- 程序调用的授权/认证续接正在接线：调用时工具及参数/结果合同写入
  客户/模型/模板绑定的加密指纹，校验子调用身份和结果因果顺序，
  custom regex/Lark在派发前进入既有隔离校验worker。9项新增及
  直接受影响CPU准备成组验证；公开接口、业务及发行仍待完成，
  rev118保持，继续全部master。

- [程序调用隔离执行部件](SERVE_PROGRAMMATIC_TOOLS_2026-11-13.md)已通过
  [13项针对性CPU](evidence/serve-programmatic-worker-cpu-20261113/README.md)：
  真实JavaScript、并行/依赖续接、结果顺序、资源限额和回收验证通过；
  构建零警告、0GPU、默认rev118保持。公开授权/持久续接/执行接线、
  真实业务与发行仍待完成，不能算对外能力已交付；全部master继续。

- PR26精确head `1282ece2`的push/PR两组CI均通过，已实际合main
  （`090f0d48`）；[回执](evidence/serve-pr26-merged-20261113/README.md)保留。

- PR26旧head一次CI暴露正常Batch恢复误用150ms故障期限；已将三项
  同类夹具的正常/注入期限分开，[7项针对性CPU首过](evidence/serve-ci-batch-recount-deadline-20261113/README.md)。
  生产和rev118字节未变，0GPU；继续最新head检查及实际合main。

- 原生工具发现已[可回退发行rev118](evidence/serve-tool-discovery-delivery-20261113/README.md)：
  17项STOPPED检查全过、0GPU，实际回退再前进，密钥/配额/core/outer
  及恢复预算保持。PR26待检查与实际合main；继续程序调用及全部master。

- 工具发现[主轻、独立回退与整树恢复均通过](evidence/serve-tool-discovery-qualification-20261113/README.md)：
  5启动14业务1探测，244次NVML正常、资源清理；真实资料结果在切换
  后继续闭环。只做17项STOPPED可回退交付，随后实际合main；全部
  master继续，默认rev110尚未改变。

- 回退准备暴露工具发现输出枚举遗漏；已[修复并通过10项CPU及实际资格迁移](evidence/serve-tool-discovery-migration-cpu-20261113/README.md)。
  主轻2启动8请求已通过；引擎/原生helper不变，继续原定独立回退与
  整树恢复短验，再可回退发行和实际合main；默认rev110保持。

- 工具发现[真实业务补验通过](evidence/serve-tool-discovery-business-20261113/README.md)：
  1启动/10创建/104.447秒，server函数JSON/custom SSE、client目录外
  定义及additional_tools四次实际读取闭环通过，错误结果400拒绝。
  98次NVML正常，资源清理；连原失败累计2/11/144.311秒。PR26已建，
  继续主轻/恢复资格、可回退发行及实际合main；默认rev110保持。

- 已用两项大词表CPU负控复现工具发现失败：仅检查拒答的guard重复
  扫描XML候选耗尽工作额度，误报内存。[修后12项针对性CPU通过](evidence/serve-tool-discovery-guard-repair-20261113/README.md)，
  grammar/拒答/停止/schema检查及资源上限保持。补验固定1启动10创建，
  含原失败累计2启动11创建1200秒；默认rev110未变，全部master继续。

- 工具发现[首次真实短验失败](evidence/serve-tool-discovery-business-failure-20261113/README.md)：
  1启动/1创建/39.863秒，首请求触发grammar资源错误。39次NVML查询
  正常，进程/端口/锁已清理，默认rev110保持。正在CPU复现语义guard
  工作预算与内存错误的混淆，修复后补原业务闭环，不把失败归档当收口。

- 工具发现公开执行链的[77项不同CPU、4项SDK/驱动检查最终通过](evidence/serve-tool-discovery-integration-cpu-20261113/README.md)：
  原位置加载、真实派发、持久结果合同、异步/推理续答、裁剪压缩及
  JSON/SSE接线已验证；失败及修正原件保留。补齐19项协议来源漏登，
  增加来源遗漏检查。接着冻结1启动10创建900秒真实业务；默认rev110
  保持，尚未宣称GPU通过或对外交付，总目标继续。

- PR25两组CI全过并已实际合main（4e87a03f）。后续[目录/历史加载部件10项CPU通过](evidence/serve-tool-discovery-catalog-cpu-20261113/README.md)，
  失败及修正原件保留；尚待公开执行器/模板/状态接线，不能视为
  tool_search已可用。继续接线和全部master；默认rev110保持，0GPU。

- [工具发现实施清单](SERVE_TOOL_DISCOVERY_2026-11-13.md)已固定：server/client
  搜索、additional_tools、按历史位置加载与权限/结果约束持续性。
  正开发原生目录及执行接线，未宣称完成；默认rev110保持。异步
  PR25（be7c2236）两组CI已过并合main，新开发保持独立。

- 异步工具已[可回退交付rev110](evidence/serve-async-tools-delivery-20261113/README.md)：
  17项实际STOPPED检查全部通过、0GPU；回退再前进、密钥/配额/
  core/outer及profile预算保持，旧默认备份保留。继续实际合main，
  随后延迟发现、程序调用及全部master，未以本阶段结束总目标。

- 发布准备发现工具历史源码哈希误作能力变化，已[修复并通过10项CPU、旧版负控及实际资格迁移](evidence/serve-async-migration-cpu-20261113/README.md)。
  默认未写入，rev102保持；仅更新主包控制模块，复用未改引擎与业务
  证据，继续17项实际停止态发布及全部master。

- 当前异步引擎的[主轻、独立回退与实际整树切换均通过](evidence/serve-async-resume-qualification-20261113/README.md)。
  5启动/14业务/1探测，三个阶段正常清理；原调用切换后仍可接收真实
  迟到结果。默认rev102保持，接着17项STOPPED可回退发行（0GPU），
  随后合主线并继续延迟发现、程序调用及全部master。

- 新引擎在回退载荷中已正确完成异步独立回答；验收比较空logprobs与
  GET默认投影时失败，[驱动修复及两项原文回放通过](evidence/serve-async-managed-projection-20261113/README.md)。
  原失败保留，仅补1启动/2业务/300秒，生产代码与已验主轻资格不变。

- 异步续答修复后的[完整JSON/SSE业务通过](evidence/serve-async-resume-business-20261113/README.md)：
  1启动/8创建/70.180秒，输出256与实际客户端边界一致，两类真实
  迟到结果均闭环。新引擎6d7d5e92正在重新取得主轻/回退资格；
  默认rev102保持，全部master继续。

- 异步续答调度及稳定历史引用[11项CPU最终通过](evidence/serve-async-resume-cpu-20261113/README.md)。
  原误答仍待真实补验；冻结1启动/8创建/900秒，JSON/SSE都按受控服务
  256输出预算验证。默认rev102保持，继续所有master。

- 异步候选[受控服务非流式短验失败](evidence/serve-async-managed-failure-20261113/README.md)：
  调用正确但续答仅省略号，1启动/2业务/50.010秒，正常清理。
  默认rev102保持；正在补内部续答回合及稳定历史引用，验证通过前不发布。

- 异步候选已取得[当前主/轻配置资格](evidence/serve-async-profiles-20261113/README.md)，
  2启动/8业务通过并清理，默认rev102保持；继续兼容回退载荷与真实
  整树切换验证。PR24已合main，async独立交付后继续全部master。

- A07异步工具[真实业务补验已通过](evidence/serve-async-business-20261113/README.md)：
  两类调用后同Response续答及custom真实迟到结果闭环通过，原函数结果
  证据显式复用；修后1启动/5创建/61.066秒，累计2/10/127.533秒。
  59次NVML全正常，模型/端口/锁清理；默认rev102保持。继续主轻配置
  资格及保持新能力的可回退交付、主线合入和全部master任务。

- 异步历史模板修复已[通过8项CPU及审计](evidence/serve-async-history-cpu-20261113/README.md)。
  下一轮冻结1启动/5创建/600秒，只补受影响路径；含原失败累计
  最多2启动/10创建/900秒，默认rev102保持。

- A07异步首轮[真实业务失败原件已保留](evidence/serve-async-business-history-failure-20261113/README.md)：
  函数闭环通过，custom续答误拒绝；1启动/5创建/66.467秒，资源已清理。
  定位到禁止新调用时历史工具定义被通用过滤器一起移除，正在修复。
  默认rev102保持，失败不改判，全部master继续。

- A07原生异步工具的[36项CPU、2项官方SDK及schema检查已通过](evidence/serve-async-tools-cpu-20261113/README.md)。
  补同Response独立回答、迟到结果、持久身份/计费和图片工具结果接入，
  失败原件保留；尚未真实模型验收或交付，默认rev102保持。继续
  冻结的1启动/8创建/900秒短验、PR24实际合入和全部master任务。

- PR24完整CI定位到custom内部Item/公开Conversation投影夹具错用，
  [6项针对性CPU通过](evidence/serve-ci-custom-views-20261113/README.md)。
  32输入及31会话形状均保留校验，生产源码/默认rev102未变；继续CI及实际合并。

- A07下一组已冻结[异步工具原生接线及验证计划](SERVE_ASYNC_TOOLS_2026-11-13.md)：
  补历史/模板/持续生成及迟到结果，不只接受async字段；程序调用与
  延迟发现保留连续任务。历史/模板/持续生成/绑定及配额已接线，12项新增与24项直接旧例
  已冻结，尚未执行；实际默认rev102保持。

- 工具命名空间/结果合同与控制器停止修复已[可回退发行rev102](evidence/serve-tool-identity-delivery-20261113/README.md)。
  17项停止态检查通过，实际回退再前进、原账本/恢复预算保持；
  原生现代根与兼容旧控制均具备新应用能力。继续PR实际合入及
  async/caller/延迟发现等全部master，不以本阶段结束总目标。

- 新工具合同与ObjectStore/CacheIo停止修复已取得[真实主/轻量资格](evidence/serve-tool-identity-profiles-20261113/README.md)，
  含同一存储会话跨重启读取/删除。兼容回退载荷的独立运行和
  [实际整树切换后工具闭环](evidence/serve-tool-identity-managed-20261113/README.md)也通过，
  合计3启动/6生成/1探测/165.913秒，全部资源清理。继续可回退发行；
  rev94保持，PR23最新17d8dbe2续验中，全部master仍active。

- PR23续验发现HTTP夹具把随机请求ID中的999误判成子用量泄漏；
  已确定性负控复现并只检查真实用量头。批任务正常存储与故障期限
  也已分开，[17项针对性CPU首过](evidence/serve-ci-trace-storage-20261113/README.md)。
  生产运行时与rev94未变；继续真实CI、合并和全部master任务。

- PR23续验定位到真实停止竞态：ObjectStore及session/prefix缓存可能
  丢失唤醒并永久占槽。[三项旧序负控复现、修后16项CPU首过](evidence/serve-controller-stop-race-20261113/README.md)。
  已将停止通知与等待条件用同一把锁；100ms等待及防增殖保护保持。
  0GPU、rev94保持，继续业务交付、实际CI/合并及全部master。

- PR23完整CI中的分页终结、正常存储与故障期限夹具已修复，
  [12项不同CPU最终通过](evidence/serve-ci-fixture-boundaries-20261113/README.md)。
  原失败及新增断言修正保留；生产预算未放宽，0GPU、rev94保持。
  继续真实CI、实际合并与全部master任务。

- A07命名空间/结果合同/标准custom语法的[真实业务已通过](evidence/serve-tool-identity-business-20261112/README.md)：
  修后1启动6请求66.366秒，含失败累计3启动10请求159.271秒。64次
  NVML全正常，模型/端口/锁已清理；继续主/轻资格与可回退发行，
  默认rev94保持，剩余A07及全部master继续。

- PR23的x86文档转换诊断已过；完整CI暴露64MiB上传容量夹具未检查
  错误回复的解引用。[补齐检查和容量IO预算后本机通过](evidence/serve-upload-capacity-fixture-20261112/README.md)。
  生产预算与故障用例未改，继续真实CI及合并，rev94保持。

- A07真实custom请求发现Responses/Chat grammar形状缺转换，已原生
  [补齐并通过7项针对性CPU](evidence/serve-responses-custom-format-20261112/README.md)。
  第二轮1启动1请求在生成前失败并清理；新引擎重验1启动6请求，累计
  上限3启动10请求，默认rev94保持。继续所有master任务。

- A07首轮函数调用/结果拒绝/真实资料JSON闭环通过；整体因SDK分页桥接
  丢游标FAILED，服务线包正确。[修复后6条CPU回放通过](evidence/serve-tool-identity-business-prefix-20261112/README.md)。
  首轮1启动3请求52.758秒已清理；只补custom的1启动3请求，rev94保持。

- A07真实引擎CPU worker暴露512MiB小于动态库映射，已改为有界1GiB，
  [实际正/负例及回收通过](evidence/serve-tool-result-worker-20261112/README.md)。
  复核候选源码审计锚点；继续1启动/6请求业务短验，默认rev94保持。

- PR23编解码修复的两个生产源码锚点已按实际diff复核并更新审计索引；
  清单与5项防遗漏负控通过，沿用16项CPU证据。继续真实CI及合并。

- A07工具命名空间/custom公开投影/调用时output_schema合同的
  [37项不同CPU及官方SDK/schema检查已通过](evidence/serve-tool-identity-cpu-20261112/README.md)。
  独立结果校验worker、存储续接及压缩历史已接线；冻结1启动/6请求/
  900秒实际业务短验，尚未执行或交付，默认rev94保持。其余A07及
  全部master缺口继续，不能把这组通过当总目标完成。

- PR23的实际退出已定位为x86旧mkdir未进有界IO代理；已补齐等价ABI，
  [16项针对性CPU通过](evidence/serve-codec-syscall-20261112/README.md)。
  文件创建仍计费、路径仍受Landlock限制，等待真实远端结果再合并；
  0GPU，默认rev94不变，继续全部master。

- A07命名空间19项不同CPU及custom公开投影两项已过，官方SDK与严格
  schema校验通过；继续调用时output_schema绑定和独立校验worker，
  冻结14项针对性检查。尚未真实模型验收或交付，默认rev94保持。

- PR23实际沙箱ready后仍有编解码退出，另三项存储/过期夹具
  [针对性CPU通过](evidence/serve-host-storage-codec-20261112/README.md)。
  原失败完整保留，远端增加有界系统调用诊断；尚未合并，rev94保持，
  继续全部master。

- PR23第二轮定位到CI的`/etc/localtime`实际mode0777及exec前父死亡
  夹具竞态；[18项Guardian针对性CPU通过](evidence/serve-host-localtime-exec-20261112/README.md)。
  临时CI路径权限与exec握手已修，生产沙箱/默认rev94保持，原两轮
  失败原件保留，等待真实远端通过再合并；全部master仍active。

- A07命名空间的定义/模型身份/JSON/SSE/历史及存储续接已成组编码，
  [结果合同的连续计划](SERVE_TOOL_IDENTITY_2026-11-12.md)保留output_schema
  与其余高级工具任务。冻结19项针对性CPU，尚未验收或交付；0GPU，
  rev94保持，PR23实际CI仍在运行。

- PR23剩余Batch夹具已[9项针对性CPU通过](evidence/serve-host-prerequisites-batch-20261112/README.md)，
  包含完整50000行/391轮/792对象；故障注入短超时不变，容量用例单独
  使用存储预算。CI的legacy资源诊断与信任权限修复已准备，等待真实
  远端结果；默认rev94保持，不宣称已合主线，继续全部master。

- R01取消清理、N05可选路由和配置schema已[可回退发行rev94](evidence/serve-cache-route-delivery-20261112/README.md)。
  缓存7请求同PID成功并恢复目标/MTP布局，含失败累计4启动15请求。
  11项STOPPED交付保持恢复预算。PR23仍修实际CI的Batch/legacy沙箱，
  尚未合主线；继续全部master任务。

- R01主/轻配置资格通过；缓存短验发现配置schema漏向量入库状态，
  [正式合同已补齐并校验3份真实响应及39负控](evidence/serve-cache-config-contract-20261112/README.md)。
  缓存业务尚未发请求，补验仅该项；累计上限4启动16请求2100秒，
  原资格复用、默认rev92保持，继续封包/CI/主线及全部master。

- Q02续修发现并实修R01取消清理等待2秒的问题，[24项针对性CPU通过](evidence/serve-public-host-repair-followup-20261112/README.md)。
  原200ms边界保持；公共测试目录、CI沙箱信任前提及旧合同夹具已修，
  继续实际CI与有界业务/封包，默认rev92保持，R01新增修复尚未交付。

- N05重审发现可选nginx入口漏同步29项后续API；[路由全集已补齐并通过7项CPU/实际双入口HTTP](evidence/serve-optional-route-parity-20261112/README.md)。
  当前66项公开API逐项闭合，续验主线/封包前保持待交付；原生默认不受影响。

- Q02 PR23公共检查的旧夹具/SDK准备已修复，[18项针对性CPU通过](evidence/serve-public-host-repair-20261112/README.md)。
  GitHub文档沙箱失败继续定位，尚未合并或宣称CI通过；0GPU、默认rev92保持。

- A11[原生资料检索已可回退发行](evidence/serve-retrieval-delivery-20261112/README.md)，
  默认rev92。实际检索、引用、历史/后台/schema、受监护版本回退及
  官方SDK原生TLS均通过；人工换根遗漏登记已修复并实际通过18项检查。
  MCP/其他内建工具仍未完成，继续全部master任务。

以下为各实施组的历史进展，当前发行结论以上方表格为准。

- A11实际发布已到rev88主包，但人工整根回退检查因驱动漏登记控制根
  恢复周期失败，保留原件，尚不宣称发行接受。已恢复已验主包，核心/
  客户端/outer/profile/version状态未被恢复操作改写。冻结最多20项
  STOPPED交接检查：明确换根、显式登记、检查再切回，旧周期完整保留；
  0GPU，不通过放宽控制根身份校验来消除失败。

- A11[受监护检索/真实回退/原生TLS均通过](evidence/serve-retrieval-managed-business-20261112/README.md)：
  含原TLS驱动失败累计5启动/10请求/1探测/314.133秒，原失败保留；
  只补TLS，SDK原始SSE及解析结果一致。自有进程、端口与锁已清理，
  下一步17项STOPPED可回退发行，默认rev80暂保持。

- A11受监护检索、版本切换及原生TLS驱动已通过
  [5项CPU](evidence/serve-retrieval-managed-driver-cpu-20261112/README.md)，
  后续分三租约累计最多4启动/8请求（含4自检）/1探测/1200秒，
  使用实际前驱控制回退payload与私有TLS副本，默认rev80保持。

- A11[实际主/轻量资格与候选封包通过](evidence/serve-file-search-profiles-20261112/README.md)：
  2启动/8模型HTTP/90.379秒，新能力声明一致；原始检索业务复用。
  候选32d3b819已含实际资格，显式v3升级许可已核对但未安装；
  默认rev80保持，继续原生TLS及受监护检索/版本恢复交付。

- A11能力发现与profile升级合同已补齐，
  [8项针对性CPU首过](evidence/serve-file-search-capabilities-cpu-20261112/README.md)。
  默认rev80保持；新增绑定未封包构建的资格入口，
  [5项CPU通过](evidence/serve-profile-build-driver-cpu-20261112/README.md)，执行
  后续2启动/8模型HTTP/900秒实际主/轻量短验，TLS/受监护交付继续。

- A11实际项目资料检索[真实业务已通过](evidence/serve-file-search-business-20261112/README.md)：
  1启动/6带模型HTTP/85.158秒，JSON/SSE/schema、实际引用、后台/历史及
  共享计量通过。原最终健康判据错误已修正，保留失败并离线复核原件，
  未重跑GPU。继续能力发现、原生TLS及受监护发行，默认rev80保持。

- A11审核识别、匿名Batch动态输入预算和取消状态已修复，
  [18项组合CPU首过](evidence/serve-file-search-combinations-cpu-20261112/README.md)。
  实际后台所有者、原ID恢复、审核持有/阻断、Batch冻结和加密/pro/
  summary/schema及连续检索通过；继续必要真实模型业务，默认保持。

- A11 Responses原生检索循环、标准事件/引用/历史、最大工具数与
  include投影已通过[34项CPU和3项SDK/schema](evidence/serve-responses-file-search-cpu-20261112/README.md)。
  原构建、夹具和Python调用问题保留，修复后仅重验受影响项；继续
  后台/Batch/审核等组合与实际模型业务。0GPU、默认rev80保持，
  尚未交付这组候选，也未完成A11或总计划。

- A11多库统一查询/加权排序与去重已通过
  [16项CPU首验](evidence/serve-vector-multi-store-cpu-20261112/README.md)，
  所有归属/索引先核验及全局预算、最终源/版本保护保持；继续
  Responses执行/事件/历史，0GPU、默认rev80保持。

- A11 Responses file_search定义/调用与混合权重已通过
  [13项针对性CPU](evidence/serve-file-search-contract-cpu-20261112/README.md)；
  首次12/13，修复精确极小数错误分类，仅重验该项。继续多库检索、
  原生工具循环/事件/引用与真实业务，0GPU、默认rev80保持。

- A11原生search/实际模型回调及共享配额已接线，
  [11项C++与3项SDK/schema首过](evidence/serve-vector-search-api-cpu-20261112/README.md)。
  官方JS直连36请求、Python原生TLS34请求；实际Files/存储，CPU
  模型夹具。0GPU、默认rev80保持，继续Responses执行及真实业务。

- A11查询执行/改写合同与搜索快照一致性已通过
  [18项针对性CPU](evidence/serve-vector-query-cpu-20261112/README.md)：实际源/完整索引、
  全查询预算、错误消费及并发版本保护；原夹具失败保留，仅重验该项。
  0GPU、默认rev80保持，继续原生HTTP/search与Responses内建循环。

- A11公开管理与内容读取已接通15条原生操作，统一schema/原生入口/
  客户端RPM及独立取消容量已接线；[12项C++和3项SDK/协议通过](evidence/serve-vector-api-cpu-20261112/README.md)。
  官方JS直连与Python经原生TLS均验证真实文件/索引文本，向量为CPU
  数学夹具；原失败保留，仅重验受影响项。实际search/Responses继续
  实施，0GPU，默认rev80保持。统一应用schema遗漏的Chat refusal
  类型也已同步，2项复用既有真实JSON/SSE检查通过。

- A11文件入库批次的持久接口已成组编码，
  [17项CPU首过](evidence/serve-vector-batches-cpu-20261112/README.md)：2000文件
  合同、原子计数/取消及批次分页；删除中的旧执行保留至实际清理，
  重挂载不能串用。继续公开HTTP/SDK/search/Responses接线。

- A11持久入库调度与真实ChatServer模型回调已接线，
  [18 项 CPU 首过](evidence/serve-vector-jobs-cpu-20261112/README.md)，实际引擎零警告构建：
  有界公平队列、原进程确认退出才恢复、取消持久化及实际清理后闭合，
  未知提交只核对原索引。辅助健康独立于核心readiness；公开API/
  SDK/Responses工具循环继续实施，默认rev80尚未变更。

- A11入库的客户端 TPM/并发许可与 BF16 输出适配已编码，
  [14 项针对性 CPU 通过](evidence/serve-vector-quota-cpu-20261112/README.md)。
  内部任务不重复外部 RPM，异常保留消费、未知用量保守结算；
  原生调度器与实际 GPU 回调继续接线，默认 rev80 保持。
- A11真实Files/隔离文档提取与tokenizer接线已编码，[11项CPU首过](evidence/serve-vector-ingestion-cpu-20261112/README.md)；
  原文件与NFC文本分别绑定，完整分块/EOS预算先准入，失败保留实际
  用量与未知提交身份。生产控制器/API/模型回调仍继续接线。
- A11持久父对象/文件关联、原子计数与索引发布、过期/回收和有界
  claim恢复状态已编码，[19项CPU已通过](evidence/serve-vector-stores-cpu-20261112/README.md)。实际提取/embedding
  执行器、HTTP/SDK及Responses仍继续实施，当前未增公开能力。
- A11原生资料检索开始成组实现：官方合同核对完成，参数、精确属性
  过滤及有界cosine/BM25排序16项CPU首过（0.026秒）；持久索引、
  API和Responses执行循环接着实现，尚未声明生产file_search可用。
  [实施与验收计划](SERVE_NATIVE_FILE_SEARCH_2026-11-12.md)，默认rev80保持。
- A11索引构建/持久化成组编码：实际token分块保UTF-8、逐块EOS和
  已消费用量，索引绑定源文件及embedding/tokenizer身份，复用不可变
  快照持久化；9新+2资产直接回归CPU首过（0.984秒）。尚未接公开存储API和
  Responses执行器，下一步继续对象管理/任务恢复/工具闭环。
- 原生外层恢复、真实资源域采集与版本回退组合已验并发行。真实root
  死亡后清树重建、未知配额保守窗口、单次文档工具、JSON/SSE与原存储
  Chat保持通过。[业务记录](evidence/serve-outer-resource-business-20261112/README.md)：
  三轮累计6启动/8Chat/1探测，严格租约283.572秒，原失败全部保留。
- 运行入口降权现在仅保留原用户已有video/render组；真实设备权限
  缺陷已修复，实际模型启动通过。[CPU与原失败](evidence/serve-runtime-identity-cpu-20261112/README.md)。
- 冷准入验收的root入口现在在永久降权子进程中核验私有策略；
  [5项CPU通过](evidence/serve-cold-owner-cpu-20261112/README.md)，未操作GPU或设备规则。
- A07模型生成拒绝已发行：Chat/Responses JSON/SSE、约束独立分支、
  真实概率、对象/历史与Python/JS客户端均接通。80项原生+19项适配
  [CPU](evidence/serve-refusal-cpu-20261112/README.md)、6请求
  [真实业务](evidence/serve-refusal-business-20261112/README.md)、
  [主/轻量资格](evidence/serve-refusal-profiles-20261112/README.md)通过。
- 新能力升级及回退已发行：修复旧启动自检对合法refusal的TypeError，
  [受监护及真实版本回退](evidence/serve-refusal-managed-business-20261112/README.md)
  累计含失败4启动/7Chat/1探测/248.927秒；拒绝对象、历史和预算保持。
  [发布副本归档](evidence/serve-publication-archive-20261112/README.md)保留
  原包及证据，0文件删除；发行后活动池31/32，归档2代可核验恢复。
- 已发布资源域约束覆盖内核memcg计量及独立物理可用内存观察，
  **没有声称实现统一DRAM/GPU物理硬上限**；R08仍须解决剩余边界。

## 全部任务进展与剩余缺口

详细实现、来源和证据以[任务表](serve-master-goals-v2.json)为准。
下列合并行只用于导航，不改变各任务的完成条件。

| 任务 | 当前状态与仍需完成的内容 |
|---|---|
| G00 | 推理/应用服务之外的组织、账单、训练管理范围待用户决定；不阻塞已确定任务 |
| A01/A02 | 已交付多指令、name、refusal历史等输入能力；剩余字段/模板及媒体、工具组合待逐项闭合 |
| A03/A04 | 采样/penalty/logprobs/multi-choice/prediction与工具选择、增量流、legacy/custom工具已验交付 |
| A05 | 丰富JSON Schema及工具/媒体组合已交付；A10媒体身份与状态缓存的组合尚待实现 |
| A06 | Completions/Models已验交付 |
| A07 | Responses核心、状态、reasoning/summary/encryption/compaction等已交付；原生refusal生产/SSE及恢复已发行；内建工具及媒体items、async/caller/namespace/output_schema等仍须实现 |
| A08 | 持久Chat/Responses、Conversations、background/cancel等已交付 |
| A09 | Files/Uploads/Batch已有实际交付；剩余8类endpoint后端及对应SDK/发行仍须与A10完成 |
| A10 | 原生embedding检索及moderation等已有交付；audio/realtime/image generation/edit/video等后端、媒体缓存及组合仍待实现 |
| A11 | 原生资料检索/Responses闭环已完成实际业务短验并发行；MCP及其余内建工具仍须实现 |
| A12 | 官方Python/JS及现有客户端的最终兼容、错误/流、迁移和发行矩阵待闭合 |
| N01—N07 | 原生接入、TLS/证书、CORS、Key/配额治理、HTTP、运维与发行入口均已验交付；N05可选前置路由已随rev94发行，主线待完成 |
| R01—R06 | 已验隔离与恢复保持；R01取消后缓存清理修复已过CPU/真实业务并随rev94发行；后续组合继续见R07—R10 |
| R07 | NVML失能冷准入代码/CPU验证已推进；实际设备独占资格、冷业务与组合验收未完成 |
| R08 | 原生资源域、压力观察与实际恢复组合已推进；物理内存硬边界及完整资源验收仍须完成 |
| R09 | 正常NVML下原生外层/资源/版本恢复已验发行；冷准入及宿主故障组合仍须闭合 |
| R10 | 视觉与恢复、降级、工具/schema组合待实现和验证 |
| Q01—Q03 | 跨接口真实资料查询闭环、最终可用主线发行、全任务核对和长稳交接仍待完成 |

## 接下来

1. 基于已修复入口与实际rev80身份重建R07冻结计划。原v1 plan-v2
   因入口缺陷及来源/默认变化而失效，不能执行。涉及nvargus/jtop的
   维护问题仍待回复，不以等待阻塞独立开发，不擅自干预其他服务。
2. [能力升级及恢复连续性](SERVE_CAPABILITY_UPGRADE_2026-11-12.md)已发行，继续A11项目资料查询的服务端工具执行/Responses事件，以及A10所需后端；先按官方合同
   和代码明确缺口，再成组实现，做针对性CPU及必要真实业务。
3. R08/R09剩余项与R10组合持续推进；各阶段可回退交付后继续下一项，
   全部计划完成前不结束总目标。

## 工作区、证据与保留候选

当前有效开发入口为主工作区的 `codex/serve-resilience-cd`。
隔离分支 `codex/serve-outer-recovery` 固定在 `38b0d29f`，已被当前工作
分支完整包含；其工作区与 `.q4t-work` 原路径继续保留，因为已发布包
与验收原件依赖这些路径。此时不删除工作区或证据。

视觉行最大值候选仍保留，未追加该候选GPU测试；范围和阻断记录见
[视觉补验结果](VISION_ROWMAX_COMPLETION_RESULT_2026-11-07.md)。
长稳实跑交给用户，短验不替代长稳结论。

此前STATUS五千多行原件完整保存在
[历史状态快照](HISTORY_STATUS_2026-11-12.md)，SHA256：
`40ae237add2d4203a4722e722a6cbfcf1925e00e7c5724e128f7bb84b15deae0`。历史结论不代表当前默认或剩余任务已完成；开发过程记录见
[当日日志](log/2026-11-12.md)。
