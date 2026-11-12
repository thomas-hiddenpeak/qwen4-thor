# 当前状态（2026-11-13）

Serve总目标仍为 **active**。按[完整计划](SERVE_MASTER_PLAN_2026-11-08.md)
和[逐项任务表](serve-master-goals-v2.json)推进全部33项任务；阶段验证与
发行是实施边界，不能替代剩余能力。非性能改动仅做受影响行为的针对性
验证和必要业务短验，长稳实跑由用户负责。

## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev110；服务包 `eeba689ceb1a0d5df0cd1e351525af5ac1806ca421b9d7cb738ed20393b2fea1` |
| Generation | `44bb0f3ca03bc7e007d2ca1242ea5796eeed23500cf3fcad1b77b93be7ea66f9` |
| 引擎 | `6d7d5e929f411d7458deaebff4654b78868f585d279c3a14c05bf158224404f3` |
| 引擎发布记录 | `82dd88855e8369031dc192589c71a7d724ac9084ef9483350a79c9a627fc0658` |
| Guardian | `383bcc63b26fe0911d1d99b455c1b4effc39b6f813e147a9cfbfb6bce5bf11dc` |
| 服务状态 | `STOPPED`；原生外层监护已配置enabled，累计启动0次 |
| 默认容量 | 单流、ctx8192、prefill2048、输出256、MTP k=3；inflight8/queue2 |
| 版本回退 | 已验旧控制/当前引擎包 `6a163a1debe64597b90a5334f8a6f1ec775536f912f0fdbe1135328f43772c91`；保持新工具能力 |
| 外部依赖 | 默认原生入口及自监护不要求nginx/systemd；宿主集成、TLS/CORS等可选配置 |

[发行结果与回退记录](evidence/serve-async-tools-delivery-20261113/README.md)
包含17项STOPPED交付检查，0新增GPU。异步函数/custom同Response续答、
迟到真实结果、当前主轻资格及整树切换均已接受；单项迁移比较修复
经CPU及实际发布验证，保持已验引擎和原生组件。
Key/配额/core/outer字节保持，profile只绑定新资格，version新周期
保留前驱。旧默认程序和状态备份保留，未宣称长稳或完整OpenAI兼容。
自动版本恢复保留现代根；人工整根回退到v12入口没有outer功能。
PR #22基础已合main（`ea0ad39a`）。PR #23的`17d8dbe2`两组CI全部通过，已实际合main（`7d00316a`），
[合并回执](evidence/serve-pr23-merged-20261113/README.md)保留。其后namespace
提交也已实际合main（PR24，`79f6e391`），
[合并回执](evidence/serve-pr24-merged-20261113/README.md)保留。
后续async提交尚待独立合入，全部master任务继续。

## 最近完成的实现和验证

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
