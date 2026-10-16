# 当前状态

更新：2026-10-17。代码是实现事实来源；验收规则见 [EVALUATION.md](EVALUATION.md)。
本入口仅维护当前决策，过程记录见当天日志与专题报告。

## 当前阶段

**活动目标：HC up TTFT因果定位与解决（用户明确设置，active）。**
以main 28a3783为生产基线，查明未调用HC新内核时200K off TTFT差异，
落实对应解决并完成受影响验收。不能以定位到区间、工具通过或增加采样
代替根因；保持N=4合同、普通decode性能与HTTP输出，复用身份不变证据。
不做无关prefill优化，不自动合并main；原HC候选NO_GO保持。
详见[目标、全部阶段与证据](HC_UP_TTFT_CAUSE_2026-10-16.md)。

新复核：干净A/B trace均有可能未收齐CUDA事件的警告，范围待核实。
窗口未截断已记录活动，但“独占”只能指与已记录活动无重叠，不证明完整。
计数复核：collected恰等于四主表，produced两侧剩余362条类别未决。
110条无kernel对应的launch全属于CUPTI worker thread，非已记录应用线程；
所有kernel均有唯一launch。worker无已记录同步，设备时长不可推断。
原六条复核无nsys包装；停止追查工具私有实现，回到未插桩运行中尚未
控制的分配/调度身份。覆盖核对：VMM控制2.684GB请求量，旧地址变化中
118项/7.667GB未覆盖（不同epoch，不作因果推断）。下一步聚焦GDN/Conv
独立状态与权重已映射36组：旧VA差仅前2/3个线性层，不等于物理/耗时证明。
当前无充分机制追加布局实验；转查MoE路由内容是否有同身份A/B证据，
不把相同launch/输出文本当作路由相同。现有4份采集均为旧身份不可复用；
冻结原A/B各一次200K off路由对照已完成，单lease COMPLETED，输出一致。
完整双检查器通过：98,298,720个prefill及122,400个decode ID含slot顺序
全部相同。停止该假设家族，耗时不验收；不能外推未插桩物理/缓存身份。
根因仍未建立。环境字段复核：旧scaling_cur_freq/5秒采样不能排除瞬态，
当前cpuinfo_cur_freq和EMC累计计数可读，但无历史前后值且计数单位待核实；
EMC time列在固定3次读取中不增长，不作为在线驻留积分。已确认OC累计
事件接口：OC2/3启用、历史count19312/109092；3次读取稳定，不能归属旧请求。
OC客户端窗口工具及runner显式入口已实现，9项CPU合同/真实只读smoke
通过；无HTTP模型执行。两端读取slack及早期decode明确记录，不将计数
当节流时长。原A/B各一次OC观察协议已冻结：单lease、各端slack≤20ms，
两条均有效完成，403次monitor无冲突。A OC2/3 +100/+301，B +76/+1020；
本次B TTFT反而短12.394ms，不能按次数归因或撤销NO_GO。下一步核对
保守、可恢复的诊断频率干预能力。GPC范围事务原语9项fake IO检查通过，
尚无真实写入。systemd FD store/ExecStopPost纯CPU normal/SIGKILL验证
通过：恢复期间锁保持、恢复后才释放，临时unit已清理。guard已接入
显式外部lease FD，22项CPU测试通过；拒绝错误/未排他持锁FD，退出
记录external_cleanup_pending并保留外部锁。监管组合3个CPU场景已通过：
normal、guard SIGKILL、模拟恢复失败等待修复；修复期间锁保持。补持久
.recovery-required门禁，23项CPU测试通过。持久ClockJournal事务9项
CPU测试及4个监管生命周期场景通过；cleanup被杀后锁会释放，但标记
拒绝后续guard，显式持锁修复后才清除。受限GPC IO已接入、4项CPU
限制检查通过；首次实机空闲单次1575→1260→1575MHz完成，5次monitor
无冲突、其他冻结字段不变、marker清除、锁释放。当前已恢复原频率。
8条原频率/降低频率交叉诊断已全部有效完成（每格2次、0重试），
1690次monitor无冲突、输出一致、原频率及lease恢复核实。降频仍有OC，
原频率B-A +152/-337ms、交互+365/-340ms，R=296.854ms；两个冻结
门禁均失败。停止1575/1260干预，不追加样本或扫频。根因尚未建立；
离线对齐同boot kernel日志：7条I2C超时中5条落入请求窗口，原频率
最快/最慢两条均无窗口内消息，计数不能归因。当前jtop持有i2c-0 FD，
不等于历史超时调用者。一次冻结300秒私有tracefs被动观测无事件、
无丢失、无同期kernel消息，instance已清理；未获得归属，停止该
采集家族，不延长或跑模型诱发。两个journal时间字段相差约431ms，
按两者分类仍相同，不能将旧偏移当精确阻塞边界。生命周期对齐
为启动后25–67秒，无统一偏移；jtop初始化有EEPROM路径，但不证明
历史调用归属。停止I2C线索扩展，不停服务或诱发请求。下一步整理
机制识别所缺的直接证据。干净trace无queued/submitted列、QMD时间
全NULL；最小3-kernel CUPTI时间戳能力验证value=3但记录0，未通过。
定位普通flush未交付：同次forced flush交付全部3条kind10，无丢失，
但submitted均UNKNOWN(0)，完整性仍失败。已核对CUPTI13.3/driver
API13.2及官方限制，未找到本例确定修复；版本不同不直接判不兼容。
不升级或换版本试跑。独占范围复核：guard driver查询覆盖compute，
不能外推历史全部图形活动；当前完整NVIDIA进程表/DRM持有者均为空，
无依据停桌面或新增隔离实验。下一步仍限能分离具体机制的直接证据。降频直接影响速度，
即使差异收敛也不单独证明OC根因；不改生产频率/保护阈值。

本轮离线核心六函数取址审计：526条变化指令中459条至少关联一处
内容/对象身份对应引用，67条未决；不代表定义所有用途或耗时等价。
抽查未决项涉及栈恢复与异常入口，均超出当前CFG工具建模，未当作缺陷。
保守栈传播六项CPU检查通过，但初始化分支经过外部调用，仍有67条未决。
停止扩建通用host等价分析器，回到设备活动证据的计时/并发可识别性核查；
无新增GPU请求，根因仍未建立，目标保持active。

最新阶段：ASLR固定8条已完成，输出/容量/退出及映射有效性全过，guard
正常结束。同variant no-ASLR代码映射重复一致，inherit重复不同；四条
no-ASLR的30条共享库映射记录亦相同。inherit B−A +326.637/+396.518ms，
no-ASLR差−33.800/+125.552ms；交互+360.437/+270.966ms，均低于冻结
2R=452.289ms门槛。方向一致仍不足以指认根因；停止此家族、不降阈值、
不把no-ASLR当部署修复。原NO_GO、生产main身份与活动目标保持。
已审计旧host PMU：user cycles候选两对均少，branch misses方向不一致；
缺cache/TLB/stall且不是ASLR同轮，不能拼接成机制证明。旧GPU trace
无SM/内存stall计数并含污染。UCF disabled事件普通用户EACCES，但已有
sudo权限下打开成功；未启用采集或改系统配置，权限不是当前阻塞。
Nsight默认串行化及缓存/clock/pipeline boost控制会改变观察条件。
UCF四事件pinned分组200ms检查通过：ID/长度正确、enabled=running，
无模型负载但原始流量非零。官方范围是GPU经UCF物理地址流量，非SM
stall。外部单窗口采集器已落地，正常/错误命令/EOF三项生命周期验证
通过，零警告；未注入服务器。已知256MiB GPU拷贝一次/四次的读流量
为268453376/1074083840，比例4.001，冻结校准通过；未校准outstanding
单位。EvalScope首有效输出钩子已实现：源码SHA/AST锚点校验，四项CPU
边界/失败测试通过。CLI封装与runner显式--ucf-probe入口已完成，6项
collector、2项集成、2项runner合同通过；缺少计数记录不能伪装成功。
UCF固定ABBA已完成：输出/计数/边界全过，guard COMPLETED。读字节
B−A为−198.65/+11.94GB，同身份R197.77GB，冻结判据未过；TTFT虽
两对+68.070/+77.039ms，不能据此归因额外读流量。停止该家族、不扩样。
HTTP首输出窗口可能含少量decode，非纯prefill；原始outstanding不当stall。
干净A/B CUDA-only trace在A启动阶段ENV_ABORT，尚未HTTP。CPU-only
复现确认Nsight目标setsid但祖先属于driver；guard仅认PGID造成自有
后代误报。已以父子链/start_ticks修复归属，用pidfd清理独立session，
清理异常返回75。19个独立CPU用例与真实Nsight CPU归属验证通过。
原失败保留；v2两条trace已完成，输出/退出/导出与launch身份均过，
guard COMPLETED。HTTP差+55.085ms，设备区间差+52.864ms=活动并集
+90.104ms、间隙−37.240ms。定位到设备活动增加，尚非原因；kernel
总和受并发重叠影响。离线拆分确认独占+41.562ms、重叠+48.542ms；
GDN/Conv/Combine各自全程独占仍变慢。Combine同48层序号累计均正，
24个完整chunk均正、末尾8181 chunk近持平。Combine三个输入均在
持久d_ws，输出ping-pong trunk；旧七scratch干预未覆盖。旧地址观察
仅d_trunk偏移512MiB，d_ws/d_trunk2 VA相同，但不是trace同轮；新trace
偶/奇层正差近似对称，不据此认定输出VA原因。下一步核实持久布局
干预可控性：小内存检查确认VMM同backing双VA读写通过（2MiB粒度），
legacy cudaMalloc retain返回INVALID_VALUE，不能直接保原页改VA。
跨顺序进程共享backing能力已通过：同一2MiB VMM分配经POSIX FD导入，
两个fresh exec子进程映射相差512MiB的指定VA，三方全量读写与清理通过，
guard COMPLETED。独立persistent_layout_probe已实现：校验1541次startup
分配序列，保留legacy分配，仅替换d_ws/双trunk返回指针；两模式均映射
相同VMM区域。零警告构建、9项fake CUDA合同通过，未接入模型。
backing owner及runner显式FD传递已实现，3项owner/4项runner CPU合同
通过（runner一次mock缺陷修正后仅重验失败项）。真实VMM三份buffer
共2.68GB有效区域跨两个进程全量验证/清理通过（legacy分配mock，非模型）。
8条original/shared对照已完成，完整guard+同一owner，输出/容量/退出全过。
R238.727ms，交互+91.711/+5.350ms均低于2R477.454ms；原路径B−A
+99.116/−173.023ms后半反向，冻结因果门槛未过。停止placement家族、
不扩样、不交付诊断allocator。旧registration覆盖已离线复核：B→C仅
构造入口4字节RET，嵌入fatbin未变；旧/干净trace都缺启动模块装载及GPU
指令地址，不能据失败注册实验排除全部指令侧因素。下一步有界观察启动期
MODULE_LOADED+cubin哈希，先确认未调用HC模块是否实际装载；该事实本身
仍不足以归因，不重复旧注册性能对照。
MODULE_LOADED观察器已实现，4项CPU合同及一次已知CUDA模块SHA校准
通过。原A/B各一次startup到ready均enabled=1/complete=0，无模块装载
事件；A正常退出后driver错误非零断言导致COMMAND_FAILED，保留并复用
A证据，仅修正校验后运行原计划B（guard COMPLETED），未重跑A。单次
B off请求观察已完成：39次模块装载、16次匹配项目镜像，HC独有SHA未出现，
输出/退出/guard通过。停止额外HC镜像装载解释；不使用本次计时。解析器
不存在request_begin标记的假设已离线修正，未重跑GPU。host实例审计已
保留A1149/B1153函数行：新暴露ExpertName差异属于已测跳板。off小M
提前返回A32/B29条指令且均不调用HC，不能据计数归因。操作数审计保守
匹配419实例，627个唯一同名/助记符相同函数仍有寻址或歧义调用变化；
2472个相邻ADRP地址变化引用中114个映射同一具名对象，2358个未决。
2358项进一步核对：2025项相同NUL结束字符串、102项相同唯一符号/别名
对象、39项相同实际宽度只读加载，剩192项（171 bss/21 rodata）。所选
六个forward函数130项均已对应。剩171个handle经注册构造函数映射到
相同完整fatbin，21处rodata按实际跨度字节一致；该批2472项相邻取址
均有内容/符号/模块身份对应，但不证明全部host或访问成本相同。735处
同名直接分支已按地址定位61对目标，加1对嵌套helper；134处常量引用
及嵌套依赖均对应，5项正常CFG合流检查通过。下一步仅聚焦off核心提交
函数非相邻取址，不据静态内容相同撤销NO_GO或新增GPU实验。
八份服务器日志已显式纳入证据，避免通用忽略规则漏收。
UCF analyzer的outputs重复哈希标注已修正，原始文本hash独立核对通过，
读流量结论不变。不得继续普通ASLR或UCF计时。
此前scratch固定布局8条判据亦未过，诊断分配器不交付、不扩样。

已完成且不重复：
- 原22份CUDA ELF相同；2264679次prefill kernel的23资源属性、四stream
  内序列相同。原trace A存在已知竞争污染，不作为干净耗时对照。
- 仅消除host跳板、仅取消HC启动注册的受控干预均未达冻结因果判据。
- Host观测、Gather观测、subtiming家族已停止；分段subtiming保留原
  ENV_ABORT与独占空档，不能当完整连续ABBA通过。
- Guard自有进程退出误报已修复；14项CPU合同/host CTest通过，之后
  多阶段监控正常完成。修复监控不等于修复TTFT根因。
- 地址调查两条输出/覆盖/guard通过；分配/free调用关系相同，119处启动
  与858处请求地址不同。完整6600条prefill映射确认后者仅routing和linear
  六项scratch，分布6个chunk；全部async alloc/free在stream0。
- 既有12条请求遥测未见采样频率变化或区间回收/压缩增加，但不覆盖
  瞬态、进程归属或物理布局。TTFT源码边界核对未发现实现错误。

这些结果不构成HC合入依据；候选尚缺的完整数值/质量项仍未验收。

**五项目综合实施计划已制定（2026-10-16；尚未实施）。**
见[分阶段计划](REFERENCE_INTEGRATION_PLAN_2026-10-16.md)：近期先复用现有
工具完成固定IDs诊断、默认MTP完整轨迹与数值/缓存分析；执行组织首项
收窄为MTP已知精确max_position下传，随后按证据选择GPU元数据/Graph。
代码复核确认draft内部仍有位置/专家counts回读和extend临时分配，
仅token驻留GPU不代表循环无同步；这些成本尚未测定。
草稿head/suffix、完整会话/真实页池/冷存储、专家预取按需求和证据触发。
计划定义代码落点、状态/输出分界、资源预算、验收与停止条件；本轮
仅改文档，未启动实现、GPU或新目标，HC活动目标和已有NO_GO保持。

**Megatron Inference补充评估完成（2026-10-16；源码研究）。**
固定上游3e2807ce，核对路由原语、实际调用链及测试：重放固定专家IDs，
仍计算当前gate/权重；推理链路主要RECORD与导出，不能宣称省掉router。
优先借鉴诊断注入、默认MTP的实际计算/已提交双视图记录，随后参考
batch-invariant负控与异步/Graph生命周期。上游也支持repeated MTP，
但GDN明确不支持speculative、prefix cache、batch-invariant组合。
见[评估报告](MEGATRON_INFERENCE_REVIEW_2026-10-16.md)及固定来源清单。
仅文档与来源校验，未构建或运行GPU/HTTP；未接入候选，不改变数值/
性能结论，不启动新的优化试验，当前HC活动目标与排程不变。

**GDN Record/Fold有界研究完成（2026-10-16）：NO_GO，GPU窗口已释放。**
隔离候选dcb2a7d/b154e4减少checkpoint写payload，分配容量不变。
默认MTP五档各3次，decode几何平均+0.680728%，但44K/200K TTFT
范围整体慢于已接受基线，触发冻结保护门槛。质量11/11、性能请求
15/15输出合同通过；两个服务退出0，正式期间未检测到竞争进程。
off、状态/生命周期、256步数值及取消恢复未运行，不宣称数值通过。
两次ENV_ABORT与第二次0条可复用结果原样保留；正式仅用http-resume。
候选历史与证据保留，生产src/include/CMakeLists及build/q4t仍为
main 28a3783/be74946d身份；不接入候选，不改main，不追加样本或候选。
见[实现与结论](GDN_RECORD_FOLD_2026-10-16.md)、
[证据](evidence/gdn-record-fold-20261016/README.md)及
[环境记录](GDN_GPU_QUEUE_2026-10-16.md)。

**HC up TTFT定位阶段记录（2026-10-16）：差异位置已缩小，根因未闭合。**
追加并发证据：A trace的长停顿全部位于另一模型加载区间，不能仅归因
Nsight；轻量A启动也有重叠，竞争服务在HTTP前29.558秒退出，但残留影响
未量化。下述耗时仅保留为观测，不构成隔离因果证明。HC本轮GPU工作
已停止；后续对照须跨会话全程互斥，不能只查启动前空闲。
固定A/B两条trace确认2264679次prefill kernel、四stream内序列/配置
完全一致，HC调用0次，频率观测固定；首次A trace秒级分配阻塞使总耗时
失真。针对该具体问题另做固定两条无Nsight事件/分配诊断：HTTP差+87.918ms，
其中层事件区间+86.07ms（线性注意力层组+116.35、全注意力层组-30.28）；
6602次分配大小序列一致，分配wall time差+19.508ms不能单独解释全部。
事件区间包含host等待，不等于纯GPU算子耗时。host地址和链接器跳板
确有变化，但其因果贡献未证明，不将原回退撤销为噪声。
四条诊断均输入/输出合同通过、服务退出0；未改产品或数值合同，未重跑
五档/候选数值，原NO_GO与main身份保留。后续需受控布局/提交时序因果
对照，尚未执行。见[定位报告](HC_UP_TTFT_CAUSE_2026-10-16.md)。

**HC up已有候选复审完成（2026-10-16）：NO_GO保留，不建议现版本合入。**
固定3对200K off交错HTTP，候选TTFT三对均慢+135/+105/+28毫秒，
均值+89毫秒（+0.0563%），两组范围重叠；按冻结规则疑点未解除。
六条HTTP输入/输出检查通过、服务退出0。静态确认该请求不进新HC up
分派、22份原CUDA ELF全部相同，故未定位差异根因，不能归咎新内核。
不追加样本、不补未运行数值/质量项；已有+0.838%仍是未接受候选收益。
生产源码/binary保持main身份，本轮仅记录，未合并main。
见[复审结论](HC_UP_REVIEW_2026-10-16.md)及
[证据](evidence/hc-up-review-20261016/README.md)。

**外部实现源码评估完成（2026-10-16；研究结论，非新优化验收）。**
已冻结并审阅KVMem、NInfer、ActKV社区实现与Strata关键代码及生产调用链。
当前decode优先参考GPU接受/元数据、GDN Record/Fold与独立草稿head；
未来会话容量参考native KV事务及保留原QSA选择的页驻留。
ActKV额外淘汰、低比特KV及驻留偏置路由属于新的近似/数值范围，未接入。
仅新增[源码评估报告](REFERENCE_REVIEW_2026-10-16.md)与文档索引，
未运行构建、HTTP/GPU/bench，未宣称新性能或质量通过；生产身份及下述
两候选NO_GO结论不变。后续候选需单独冻结范围，不自动追加第三次试验。

**MTP decode有界优化收尾（2026-10-16）：两候选NO_GO，无新优化接受。**
以main 28a3783为基线，按用户授权先HC up再独立T4 attention，最多两候选。
HC up默认MTP五档几何平均+0.838%，但off 200K TTFT保护门禁未过；
T4 softmax指数并行五档几何平均-0.0116%，无可辨识吞吐收益，且on 200K
TTFT保护门禁未过。目标+5%未达成，不追加第三候选或放宽阈值。
两候选质量on均11/11、on五档各15/15输出与main相同；HC up另完成off
15/15。后续数值/诊断未运行，不能宣称候选数值通过。共67条HTTP，
没有重跑main或bench/profile；所有失败/候选提交、二进制与原始证据保留。
生产源码恢复main，build/q4t恢复原始SHA be74946d8159…；相同身份的
主线数值、质量与性能证据直接复用，不为回退重复测试。
详见[阶段报告](MTP_DECODE_2026-10-16.md)及
[HC up证据](evidence/mtp-decode-20261016/hc-up/README.md)、
[T4 attention证据](evidence/mtp-decode-20261016/attention/README.md)。
工作分支保留完整阶段提交并推送，main不变。下方主线交付仍是生产基线。

**MTP主线交付版本（2026-10-16；本节随PR #4合入main生效）**。
用户已授权将验收成果以合并提交进入main，保留完整阶段历史；
[PR #4](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/4)记录实际合并SHA与状态。
合并前main为`7f6a69c`，验收成果提交为`2631d8d`；本轮仅补充文档，
生产代码与验收身份不变。工作分支`codex/moe-residency-20260930`保留，
继续作为后续性能基础；不删除研究分支、工作区或本机证据。

五项诊断`model_verify_ckpt`、`model_verify_multi_ckpt`、
`model_verify_ckpt_samepath`、`model_prefill_batch`、`mtp_spec_accept_states`
均通过（5/5、0 skipped）。以独立已接受前缀/同M因果前缀作逐位门禁，
包含partial/full状态、输出与错row负控；原跨形状L2/argmax差异及失败证据
保留，不提高阈值，不声称任意计算形状逐位不变。

实际修复PLE pinned staging异步复用：受控首输出160/160错误归零；
同stream、跨stream、析构及CPU参考四项通过。完整256步N=4近tie通过：
4 flips、0双侧confident、causality PASS。两项按未变化的运行时身份复用。

新binary `be74946d8159…`：off=`--no-mtp`、on=默认k3，五档各3次，
decode增益 **+85.2%/+98.4%/+106.1%/+100.8%/+93.5%**，全部在+50%～+200%；
确定性、HTTP输入/输出/finish合同全过，十组文本与修复前一致。off绝对
吞吐较前次测量变化−0.36%～−0.09%，on为−0.05%～+0.15%。

HTTP固定检索两侧各11/11；六项尾部请求、流式取消及后继恢复通过；
服务退出正常，最终测试构建零警告。质量数据库没有保存HTTP状态码，
按其成功标志与明确来源验收；尾部脚本保留直接HTTP状态。最终CI见PR检查。

报告：[数值收口与影响范围](MTP_NUMERICAL_CLOSURE_2026-10-16.md)；
[最终证据](evidence/mtp-numerical-closure-20261016/phase04/README.md)。
`9be8478`/`9879cda`/`21226b2`划分诊断、运行时修复及正式合同阶段。
运行回退使用`--no-mtp`并保留PLE修复。源码归并边界由PR的合并提交及其
第1父提交`7f6a69c`标识；旧实现含已知PLE缺陷，不作为新的正确性基线。
合入后核验main包含验收提交、树身份及主线CI；不将有限
单流/受控小模型结果扩大为全量GPU测试、任意批量不变或模型任务能力保证。

## 历史阶段记录（以前述当前阶段为准）

**新 Goal：MTP 性能成果推进到可合入主线 main（2026-10-15；取代下方各节为最新状态）**：
以当前工作分支 `codex/moe-residency-20260930`（HEAD `03339bf`）为后续性能基础，分三步：
(1) 决策两套 MTP 实现归并边界；(2) 复确认五档 E2E 性能基础；(3) 近 tie 合同与主流引擎
对照补进 EVALUATION.md。
**Step 1 完成**：两套 MTP 归并边界已决策，见 [MTP_MERGE_BOUNDARY_2026-10-15.md](MTP_MERGE_BOUNDARY_2026-10-15.md)。
核心结论：两分支自 merge-base `8ea85b1` 各自独立演化，对同一 8 文件分叉。main sequential
verifier 用普通 scheduler **B1** 算术（与 plain 逐位一致、无数值分岔、慢 10–12%、默认关）；
当前分支 T>1 用 **T=k+1** batch 算术（近 tie 分岔、快 +85%~+107%、默认开、近 tie 合同
N=4 ULP PASS）。**推荐归并策略 A（替换）**：归并后默认 MTP=T>1；sequential 不并入当前
分支（与普通 B1 算术强耦合，并入需重写 mtp.cu 双路径），其正确性参考价值保留在 main 历史。
近 tie 合同与主流引擎（vLLM 默认非 batch-不变、NeurIPS 2025/LLM-42 浮点非结合性）行为
一致且更严。
**Step 2 完成**：五档 E2E 复确认通过（同 binary d4a223df，证据
.q4t-work/mtp-merge-e2e-20261015/，off=`--no-mtp` port 8730 vs on=serve 默认 T>1
port 8731，各 3 次，--fixtures 复用 2026-10-14 记录边界提示）：decode_tps
off→on 1024: 18.63→34.51（+85.2%）/ 4096: 17.86→35.36（+98.0%）/ 8192:
18.11→37.61（+107.6%）/ 45056: 17.86→36.61（+105.0%）/ 204800: 17.11→33.11
（+93.5%），全部在 +50%~+200% 目标区间；两侧 3 次逐字节确定性、输入/输出/
finish 合同全过（http_output_checks_passed=true）；off 侧与 2026-10-14 五档
验收 ±0.5% 内。
**Step 3 完成**：近 tie 合同与主流引擎对照已补进 EVALUATION.md「近 tie 合同
与主流引擎对照（2026-10-15）」。
边界：未改产品代码；归并前 main 保持不动（回退点 `7f6a69c`）。
下一步：按 MTP_MERGE_BOUNDARY §8.4 执行归并（git merge main，27 冲突文件按
§8.1–8.3 裁决，删 sequential 专属工件，构建零警告，归并树五档 E2E 复确认，
阶段快照 commit+推送）。

**MTP 默认开启五档正式验收通过：五档全部 +50%~+200%（2026-10-14 晚；取代下方各节为最新状态）**：
执行 45K 口径决策 (b)：性能档提示改为以完整记录为边界收尾（prepare_inputs.py
--snap-records；45K 档不再携带 "Record 136" 构造残片；实际输入至多比名义档长短
一条记录，验收按 [名义-48, 名义] 区间核对；limits 档保持精确长度）。五档正式
验收（同 binary d4a223df、commit ba5f117、off=--no-mtp vs on=serve 默认无旗标，
各 3 次，证据 .q4t-work/mtp-snap-e2e-20261014/）：decode_tps off→on
1024: 18.65→34.51（+85.0%）/ 4096: 17.89→35.33（+97.5%）/ 8192:
18.13→37.61（+107.4%）/ 45056: 17.86→36.56（+104.7%）/ 204800:
17.13→33.04（+92.9%），各档确定性（3 次逐字节一致）、输入/输出/finish 合同
全过，on 侧服务器日志确认 mtp=1 默认路径。45K 档 +45.7%→+104.7%，输出形态
回到 thinking 块；低接受率根因确认为提示构造残片（E2E 提示构造客观条件），
非 runner 缺陷（与下方六轮 45K 路径 bit-exact 核验一致）。
边界：五档矩阵提示身份已改为记录边界版，历史参考数字仅 off 侧可比（±0.5%）；
on 侧 1024/4096/8192 较旧矩阵低约 1-5%（末条记录内容变化所致），仍远高于
+50%；近 tie 契约容差已决策（N=4 ULP，EVALUATION.md「MTP vs plain 近 tie
合同（2026-10-12）」，mtp_verify_numerics 256 步 PASS：flips=4/256 全部 1–2 ULP
近 tie、confident_both_sides=0、causality 负控通过、min_nonflip_gap=0.125=1 ULP，
证据 .q4t-work/verify-nums-ulp-rerun2/run.log），45K off/on 输出分歧属已知近 tie
翻转类（合同内）。
**目标达成**：MTP 正确性达可默认部署状态（默认开启 use_mtp=true + 五档 E2E 通过
+ 近 tie 数值合同通过 + 45K 路径六轮 bit-exact 干净）；decode 提速 50–200% 目标
达成（五档 +85.0%/+97.5%/+107.4%/+104.7%/+92.9%）。
下一步：(a) 阶段快照已 commit 并推送（c741d06）；(b) 近 tie 契约容差已决策（N=4）；
(c) 如需进一步提速可继续每步优化（可选，不阻塞）。无遗留项。

**45K 档 draft-model 分块 draft-extend 直接核验：逐位正确，45K 路径第六轮闭合（2026-10-14 晚；已被上方节取代）**：
补上前五轮未覆盖的 45K 特有路径——draft 模型的分块 draft-extend（mtp.cu
MtpDraftExtend chunked 分支，45056 > max_prefill 按块续写 KV；主模型 chunk
invariance 测不出此路径，verify 会纠正 draft）。同 E2E 45K 提示（含 "Record 136"
残片，低接受率档）、同 binary d4a223df、MTP on k=3、max-seq 1、256 token、
Q4T_MTP_DEBUG（证据 .q4t-work/45k-draft-chunk-20261014/）：对比 max_prefill 4096
（11 块）vs 8192（5 整块 + 1 部分尾块 4096，45K 特有部分尾块条件），两档各 99 步
[mtp-debug2]（draft 3 token + main 3 token + corr）**逐位一致**，接受分布相同
26×a=0/19×a=1/24×a=2/30×a=3 → mean_a 1.586（tok/步 2.586），输出全文逐字节一致。
**判定：45K 档 draft 分块 draft-extend（含部分尾块）逐位正确，无 45K 特有缺陷。**
至此 45K 路径（主模型 + draft 模型）全部核验干净：分块 prefill/KV 分页/稀疏注意力
top-512 选择/MoE/draft-verify 数值/尾部读取/draft 分块 draft-extend。低接受率根因=
E2E 45K 提示构造残片（"Record 136"）使模型产出低可预测直接综述（mean_a 1.59；去残片
同长度 2.28），属 E2E 提示构造客观条件，非 runner 缺陷。
边界：未改产品代码；MTP 默认开启；两候选 env unset。
下一步（待用户决策 45K 档口径）：(a) 接受 45K +45.7% 归档（路径证据六轮 bit-exact +
受控实验，充分闭合）；(b) 45K 档 E2E 提示改干净版（无残片，mean_a 1.59→2.28，
decode_tps 预计显著提升、可能过 +50%）；(c) 继续每步优化推 45K 过 +50%（需再省
~2.9ms/步）。

**45K 档受控长度实验（固定内容、变长度）：接受率随形态、非单调，长度路径 bug 进一步排除（2026-10-14 晚；取代下方各节为最新状态）**：
按用户"出问题的上下文长度路径有问题"假设做真正受控实验：固定干净 1360 条内容、
中性填充只变长度（45050/86355/127693 与 45050/101192/157352 tok 两种填充设计，
同 binary d4a223df、MTP on k=3，证据 .q4t-work/45k-lencontrolled-20261014/）：
mean_a 随长度**非单调**（设计2：2.28→1.49→2.81，157K 档反而最高），且与输出形态
强相关（thinking ~2.3-2.8 / 直接综述 ~1.5），与此前各轮一致。若存在系统性长度路径
bug，mean_a 应随长度单调下降；实际非单调且形态相关，**强烈排除长度路径 bug**。
诚实边界：填充非完全中性（输出随填充变），非完全隔离受控实验，但证据方向明确。
至此 45K 路径证据=五轮 bit-exact 核验（分块/KV/稀疏注意力 top-512 选择含 45K 特有
部分尾块/MoE/draft-verify 数值/尾部读取）+ 本轮受控实验，充分闭合：45K 路径干净，
低接受率根因=E2E 45K 提示构造残片+记录数少（内容/形态驱动），非 runner 缺陷。
边界：MTP 默认开启；Q4T_DUMP_TOPK debug 钩子默认关闭、未提交，待 45K 口径决策后
一并处理；两候选 env unset。
下一步（待用户决策 45K 档口径）：(a) 接受 45K +45.7% 归档（推荐，证据充分闭合）；
(b) 45K 档 E2E 提示改干净版（无残片，mean_a 1.59→2.28，decode_tps 预计显著提升、
可能过 +50%）；(c) 继续每步优化推 45K 过 +50%（需再省 ~2.9ms/步）。

**45K 档 top-512 选择路径直接核验：逐位正确，45K 路径第五轮闭合（2026-10-14 晚；取代下方各节为最新状态）**：
**45K 档 top-512 选择路径直接核验：逐位正确，45K 路径第五轮闭合（2026-10-14 晚；取代下方各节为最新状态）**：
按用户要求对 45K 档（2.59 tok/步，唯一未达 +50%）单独做针对性路径分析，第五轮补上
45K 特有的 top-512 块选择（部分尾块 11262=5×2048+1022）对独立 ground truth 的直接
核验（debug-only 观测 Q4T_DUMP_TOPK，默认关闭、不触碰计算；证据
.q4t-work/topk-verify-20261014/）：45K 与 200K 的 kernel 513 唯一块 == GT top-512 +
强制尾块，token 集合逐位相等，512 截断处无边界 tie。**选择逐位正确，无选择缺陷。**
至此 45K 路径（分块 prefill/KV 分页/稀疏注意力 top-512 选择/MoE/draft-verify 数值/
尾部读取）五轮全部核验干净：无长上下文退化，无 45K 特有缺陷。低接受率根因=E2E 45K
提示的构造残片（"Record 136" 部分记录标签，字符边界截断落在记录标签中间）使模型
产出低可预测直接综述（mean_a 1.59；去残片同长度 2.28），属 E2E 提示构造客观条件，
非 runner 缺陷。
边界：Q4T_DUMP_TOPK debug 钩子默认关闭、未提交，待 45K 口径决策后一并处理；
MTP 默认开启；两候选 env unset。
下一步（待用户决策 45K 档口径）：(a) 接受 45K +45.7% 归档；(b) 45K 档 E2E 提示改
干净版（无残片 1360 条，mean_a 1.59→2.28，decode_tps 预计显著提升、可能过 +50%）；
(c) 继续每步优化推 45K 过 +50%（需再省 ~2.9ms/步）。

**45K 档定向路径分析（第四轮）：翻转驱动=部分记录残片，尾部路径核验干净（2026-10-14 晚；取代下方各节为最新状态）**：
用户继续质疑 45K 档接受率并要求单独针对性分析其路径。补做"翻转驱动精确定位 + 尾部路径
直接核验"（同 binary bb8fd0bf、MTP on k=3，证据 .q4t-work/45k-path4-20261014/）：
(1) 翻转驱动定位——同 45K 长度 review 任务：E2E 45K（1360 条 + "Record 136" 残片）→
空 thinking + 直接综述 mean_a 1.59；去掉残片（1360 条干净，A/B 两构造逐字节相同）→
实质 thinking 块 mean_a 2.28。判定：部分记录残片（字符边界截断落在 "Record 1361:" 标签
中间）是翻转驱动。(2) 尾部路径直接核验——序列探针（逐字列出 Record 1355-1360）：45K 与
200K 对同一尾部 6 条记录读取逐字一致、顺序正确。判定：45K 尾部路径（分块 prefill/KV
分页/稀疏注意力）干净，无 45K 特有缺陷。**结论：45K 路径干净，低接受率根因=E2E 45K 提示
的构造残片使模型产出低可预测直接综述，属 E2E 提示构造客观条件，非 runner 缺陷。**
边界：未改产品代码；MTP 默认开启；两候选 env unset。
下一步（待用户决策 45K 档口径）：(a) 接受 45K +45.7% 归档；(b) 45K 档 E2E 提示改干净版
（无残片，mean_a 1.59→2.28）；(c) 继续每步优化推 45K 过 +50%。

**MTP 默认开启（k=3）+ 五档 E2E 验收（2026-10-14；取代下方各节为最新状态）**：
serve 现默认开启 MTP 投机解码（k=3），`--no-mtp` 回退普通 greedy decode
（性能基线侧）；run_acceptance.py 新增 `--mtp-default`（不带 MTP 旗标，验证
serve 默认路径）。证据（.q4t-work/mtp-default-e2e-20261014/，同 binary
bb8fd0bf，off `--no-mtp` vs on `--mtp`，各 3 次，确定性）：decode_tps
1024: 18.60→36.24（+94.9%）/ 4096: 17.91→36.85（+105.8%）/ 8192:
18.19→38.08（+109.3%）/ 45056: 17.86→26.02（+45.7%）/ 204800:
17.18→32.63（+89.9%）。Smoke（.q4t-work/mtp-default-smoke-20261014/）：
默认路径（无旗标）输出与显式 `--mtp` 逐字节一致。4/5 档达 +50% 目标
（在 50–200% 目标区间内）；45K 档 +45.7% 归因客观内容/接受率条件
（1359 条记录提示 → 空 thinking + 直接综述、可预测性低、mean_a 1.59），
路径已双向核验干净（见下方 45K 长度解耦实验节，仍为有效证据），非 runner
缺陷。
边界：MTP 现默认开启；性能基线侧为 `--no-mtp`；近 tie 契约容差仍为
EVALUATION.md 遗留项。
下一步：(a) 以每步优化把 45K 档推过 +50%（需再省 ~2.9ms/步，verify
attn≈38ms / moe≈21ms 为主目标）；(b) MTP 默认开启五档正式验收（off 基线
+ on 默认，各 3 次）；(c) 近 tie 契约容差另行决策。

**45K 档长度解耦决定性实验完成：接受率随输出形态不随长度，45K 路径双向闭合（2026-10-14 下午二；取代下方各节为最新状态）**：
用户质疑 45K 档接受率（2.59 tok/步）低于 200K 档（3.63）并要求单独
分析其路径。补做此前缺失的反向长度实验（同 binary 77a58c34、两候选
env unset，证据 .q4t-work/45k-len-decouple-20261014/）：(1) 45056 两次
运行 99 步分步轨迹逐位一致、mean_a 1.59 全程均匀（first10 1.50 /
last10 2.00），无随步数退化、无步特异坍塌；(2) 确认 E2E 45K 提示 =
200K 提示字符边界截断（完整记录 1359 条）；(3) 同内容族同截断方案
扩展：**90K（2709 条）mean_a 1.58**（输出形态与 45K 相同：空 thinking
+ 直接综述）、**180K（5399 条）mean_a 2.67**（形态翻回 thinking 块，
~20 步后稳定 a=3，与 4K/8K/200K 一致）。判定：**接受率由输出形态决定
（thinking ~2.6-3.5 / 直接综述 ~1.6），形态由提示内容记录数带决定
（≤~1339 thinking / ~1360-2709 直接综述 / ≥~5399 thinking），上下文
长度无独立效应**。45K 路径（分块 prefill 含部分尾块、KV 分页、稀疏
注意力、MoE、PLE 流式、draft/verify 数值）经两个长度方向 + 同长度异
内容 + 45K 数值验证 + 分块不变性（逐位一致）全部核验干净，无长上下文
退化、无 45K 特有缺陷。+43% 根因：该档提示恰落在低接受率内容带，属
E2E 提示选择的客观条件。45K off/on 输出分歧与 2026-10-11 已决策的
近 tie 翻转类一致（4/5 档存在、早期步单 token 近 tie、两侧确定性），
非新问题；近 tie 契约容差仍为 EVALUATION.md 遗留项。
边界：未改产品代码；MTP 仍默认关闭；两候选 env unset。
下一步（待用户决策 45K 档口径）：(a) 接受 45K +43% 为客观结果并归档
（推荐，路径证据已双向闭合）；(b) 若坚持五档均 +50%：换 45K 档提示为
thinking 形态代表（200K 截断 1339 条版 tok/步 3.46）或再省每步
~6-11ms（grouped MoE 已省 1.7ms，剩余候选小）；(c) 达标后 MTP 默认
开启五档正式验收；近 tie 契约容差另行决策。


**T>1 MoE decode 默认切换为按专家分组（2026-10-14；取代下方各节为最新状态）**：
T>1 verify 的 MoE decode 默认路径由 per-row 切换为按专家分组（moe_decode.cu；
T==1 仍 per-row 逐位不变；Q4T_MOE_DECODE_PERROW 回退 per-row 作参考/调试）。
依据：grouped 对 per-row 逐位一致（moe_decode_grouped_vs_perrow max_ulp=0.00、
routingA/B exact=10240/10240；moe_decode_t1_dispatch_bitexact PASS）；有效 A/B
（.q4t-work/moe-ab2-1013/，4K 128 token 同 binary）grouped 省 ~1.7ms/verify
forward（verify total 78.0→76.3ms，moe-subtotal 18.7→16.9ms）。
边界：未做五档 E2E 正式验收（随 MTP 默认开启统一验收）。
下一步：MTP 默认开启 + 五档 E2E 正式验收（off 基线 + on 默认，各 3 次）。

**45K 档定向路径分析（第二轮）完成：路径核验正常，异常归因内容/输出形态；off/on 分歧属已知近 tie 类（2026-10-14 下午；取代下方各节为最新状态）**：
按用户要求对 45K 档（唯一未达 +50%，200K 档 +90%）做定向路径分析，
核查"出问题的上下文长度的路径"是否异常。证据 .q4t-work/（同 binary、
两候选 env unset）：(1) 同 E2E 提示五档接受率（45k-path-targeted-2/）：
4K/8K/200K tok/步 3.74/3.69/3.63，45K 2.59（两次逐位一致）；
(2) 分块不变性：同一 45056 提示 max_prefill 4096 vs 8192，MTP off/on
输出均逐位一致——45K 是唯一"部分尾块"档（5.5×8192），该路径已验证；
(3) 截断扫描：同一 45K 提示截断到 32k/40k → thinking 块、mean_a
2.37/2.36，完整 45K → 直接综述、mean_a 1.43，尾部内容翻转输出形态；
(4) 记录数扫描：同内容族 127/300/700 条 → thinking 块，1360 条（即
E2E 45K 提示）→ 直接综述；(5) gap 特征化：首次失配 logit gap 分布
各档同量级（45k 中位 0.875），confident 占比不系统性偏高，无系统性
错误签名；(6) mtp_verify_numerics 45K 64 步 PASS（flips=2/64 全部近
tie ≤2 ULP，confident_both_sides=0，causality 负控通过）；
(7) MTP off/on 输出对照：4/5 档（1024/8192/45056/204800）off≠on、
4096 相同，分歧点均为早期步近 tie 翻转，两侧各自确定性——与
2026-10-11 T 路径算术特征化及 2026-10-13 E2E"已知近 tie 分歧
（契约内）"记录一致，非 45K 特有缺陷。
**结论：45K 路径（分块 prefill/KV 分页/稀疏注意力/MoE/draft/verify
数值）工作正常，无长上下文退化；+43% 由该 E2E 提示输出形态（直接
综述、无 thinking 块、可预测性低）决定，属客观内容条件，非 runner
缺陷。45K off/on 输出分歧属已知近 tie 翻转类（4/5 档存在）；近 tie
分歧契约容差在 EVALUATION.md 仍未定义（2026-10-11 遗留项）。**
边界：未改产品代码；MTP 仍默认关闭；两候选 env unset。
下一步（待用户决策 45K 档口径）：(a) 接受 45K +43% 为客观结果并归档
（推荐）；(b) 若坚持五档均 +50%：换更具代表性的 45K 提示（thinking
块形态）或继续压每步成本（需再省 ~11ms，难）；(c) 达标后 MTP 默认
开启五档正式验收；近 tie 契约容差另行决策。

**45K 档接受率定向分析：内容/输出形态效应，非 45K 路径缺陷（2026-10-14 上午；取代下方各节为最新状态）**：
对 45K 档 +43%（唯一未达 +50% 档）做定向对照实验，隔离"长度路径"与
"提示内容"：(1) 复用 2026-10-13 未入档的 200K 接受率对照（38 步
tok/步 3.39，前 ~20 步后稳定 a=3）；(2) 新建 45K 长度对照
（.q4t-work/accept-45k-ctrl/，200K E2E 提示截断到 ~45025 token，同
binary/参数）：37 步 tok/步 **3.46**，输出为 thinking 块（高度可预测）。
对照 E2E-45K 提示（1360 条记录，54 步 tok/步 **2.43**，输出为直接流畅
综述、无 thinking 块）。判定：接受率随提示内容/输出形态变化，不随上下文
长度变化；同为 45K 长度不同提示 3.46 vs 2.43。每步成本与长度无关
（~102.8ms，4K/45K/200K 一致）。**45K 路径工作正常，无长上下文退化；
+43% 由该特定提示输出形态导致，属客观条件，非 runner 缺陷。** 此前
"长上下文 draft 质量下降"归因不成立（200K 接受率更高已证伪）。
边界：未改产品代码；MTP 仍默认关闭；两候选 env unset。
下一步（待用户决策 45K 口径）：(a) 接受 45K +43% 为客观结果并归档
（推荐）；(b) 若坚持五档均 +50%：换更具代表性的 45K 提示，或继续压每步
成本（需再省 ~11ms，剩余候选小，难）；(c) 达标后 MTP 默认开启五档正式验收。

**MoE 按专家分组 decode（T>1）实现 + 有效 A/B（2026-10-13 深夜，已提交；取代下方各节为最新状态）**：
T>1 按专家分组 MoE decode 落地（moe_decode.cu，opt-in Q4T_MOE_DECODE_GROUPED，
默认仍 per-row）：device 端 R=T*k 行按命中专家 compact，每活跃专家一次
M=M_e grouped GEMM（权重只读一次），fixed-R cols=0 跳过未命中组，U 不回
host。数值：moe_decode_grouped_vs_perrow PASS（routingA/B exact=10240/
10240，max_ulp=0.00 逐位一致）；moe_decode_t1_dispatch_bitexact PASS。
有效 A/B（.q4t-work/moe-ab2-1013/，4K 128 token 同 binary）：grouped 省
~1.7ms/verify forward（verify total 78.0→76.3ms，moe-subtotal 18.7→16.9ms）。
此前 moe-ab-1013 A/B 无效（grouped 侧未设 env 实为 per-row）已作废。
判定：真实小赢但小于 unique/rows≈0.687 估算的 ~4.6ms（per-row 重复专家
权重已部分命中 L2）。45K 档过 +50% 仍需再省 ~2.9ms（grouped 后 per-step
93.6ms，目标 ≤90.7ms）。
边界：MTP 仍默认关闭；Q4T_MOE_DECODE_GROUPED 默认 unset；未做五档 E2E
正式验收。
下一步：(a) 再找 ~2.9ms（draft 15.5ms / HC up_gemm 4.78ms 38–40% 峰值 /
full_core 10.5ms 候选）；(b) 45K 档带 MTP 计时复跑确认过 +50%；(c) 达标
后 MTP 默认开启五档正式验收。

**五档 E2E MTP off/on 首次完整对照 + MoE GEMM 定性（2026-10-13 本轮，取代下方各节为最新状态）**：
在 5330e50（含本日全部算子优化：HC down Scattered 3x、HC inject 多行
GEMV、small-T 列分裂注意力）上完成首次完整五档 evalscope HTTP E2E
off/on 对照（.q4t-work/mtp-e2e-20261013/，两侧同 binary，env unset）：
decode_tps off→on：1024: 18.5→35.7（+93%）/ 4096: 17.86→36.3（+103%）/
8192: 18.1→37.4（+107%）/ 45056: 17.86→25.5（+43%）/ 204800:
17.05→32.3（+90%）。五档确定性、HTTP 输出检查通过；4/5 档达 +50%
目标，45K 档 +43% 略低于下限（唯一未达标档，原因未定位——该 E2E 未开
MTP 计时，无 45K 分步/接受率数据）。
MoE GEMM 定性（tools/moe_gemm_bench 新增 + nsys 实采
.q4t-work/moe-nsys-171608/）：grouped NVFP4 GEMM 每层 gemm_gu ~229us /
gemm_dn ~118us，与 bench prod 模式（512 专家分散指针 + L2 flush）
0.30/0.16ms 一致，均 ~1.2x HBM 读下限（~246 GB/s）。生产 sub-timing
gemm_gu ~11ms 为 36 层每 forward 之和，非单次调用；grouped GEMM 无
结构性低效，MoE 剩余空间只在专家权重 L2 驻留（独立工作流）。
边界：MTP 仍默认关闭；两候选 env unset；bench 工具未提交。
下一步：(a) 45K 档定位（带 MTP 计时+子计时跑 45K，取分步与接受率，
区分客观接受率条件与长上下文 verify 成本）；(b) 专家权重 L2 驻留；
(c) 达标后 MTP 默认开启的五档正式验收。

**small-T 稀疏注意力改走列分裂 kernel（T<=8，2026-10-13 本轮，已提交；取代下方各节为最新状态）**：
QsaDecodeSplit 泛化到 T（grid=(T,2,4)），full_attention 派发 T<=8 走列分裂
kernel（原仅 T==1），T>8 prefill 仍走通用 SparseAttentionKernel。逐 token 注意力
与 T=1 decode 逐位一致，消除 QSA 这一 T 依赖源；T=4 时 8 block→32 block 占满
20 SM。mtp_verify_numerics 256 步 PASS（flips=4/256 与改前一致，≤4 ULP 契约）；
生产 4K：full_core 11.0→10.3ms（-6%），verify total 76-78ms 持平，wall_s 7.2。
边界：MTP 仍默认关闭；两候选 env unset；未做五档 E2E 正式验收。
下一步：(a) MoE 专家权重流量（verify 最大单项 21-37ms，专家缓存/驻留，独立
工作流）；(b) 优化后 MTP on 五档 E2E 性能+质量正式验收。

**MTP verify 阶段两项 HC 优化落地（2026-10-13 本轮，已提交）**：
verify（T=4）细分计时定位后落地两项算子级优化，均恢复 T 路径与 T=1 GEMV 的
逐位对齐（非 cuBLASLt 过渡）：
1. HC down（N=320, K=10240）重新路由到手写 Scattered small-M kernel
   （small_m_gemm.cu 形状策略 N<512→N<320）。依据 v3 bench
   （tools/small_m_gemm_v3_bench，L2-defeated，M=4）：Scattered 50.9us vs
   cuBLASLt 151.3us（3.0x）；此前 v2 bench 对 N<512 误测 KSplit 变体导致
   误判回退。生产实测 down_gemm 14.0ms→4.1ms/verify forward（96 次调用）。
2. HC inject（N=4, K=10240）T=2..8 走新增多行 GEMV
   （HcInjectGevGatedMultirow，gemv_hc_inject.cu），每行与 T=1 融合 GEMV
   逐位一致；替代 cuBLASLt（~129us/次）。生产实测 inject 12.3ms→1.7ms。
- 验证：gemm_t1_vs_tk1_general/hc_inject + smallm_gemm_wiring 全 PASS
  （HC down/up 路由形状 T=4 行 vs T=1 GEMV 逐位一致 count=0；多行 vs
  单行 GEMV count=0）；mtp_verify_numerics 256 步 PASS（binary
  253502cf…，证据 .q4t-work/verify-nums-smalln320-145652/）：flips=4/256
  全部近 tie（1–2 ULP，≤4 ULP 契约）、confident_both_sides=0、causality
  负控通过。
- 生产计时（Q4T_LAYER_SUBTIMING，4K 上下文，证据
  .q4t-work/verify-subdetail-smalln320-150302/）：verify 101ms→~80ms
  （-21%）；wall_s 8.6→7.25（128 token）。当前 verify 构成：
  moe 22–25ms（gemm_gu ~12 + gemm_dn ~6，专家权重流式、近 DRAM 下限）/
  lin_inproj 15.5ms（78–82% 峰值带宽）/ full_core 11.2ms（12 层，
  SparseAttentionKernel grid=(T,nkv)=8 block，欠并行）/ lin_outproj 6.8ms /
  HC up 4.7ms（K=320 延迟受限，38–40% 峰值）/ 其余 <3ms。
- 边界：MTP 仍默认关闭；两候选 env unset；未做五档 E2E 正式验收。
- 下一步：(a) full_core——T>1 稀疏注意力按 KV 分段并行（仿 T=1
  QsaDecodeSplit 的 split 思路），把 8 block 扩到占满 20 SM；(b) MoE
  专家权重流量（24ms 近 DRAM 下限，需专家缓存/驻留，独立工作流）；
  (c) 优化后做 MTP on 五档 E2E 性能+质量正式验收。

**step26 writer-row0 独立捕获 + 同次 production restore 对照完成
（2026-10-11 本轮，取代下方六格描述为最新状态；本轮已收口）**：
**step26 writer-row0 独立捕获 + 同次 production restore 对照完成
（2026-10-11 本轮，取代下方六格描述为最新状态；本轮已收口）**：
mtp_writer_row0（binary e7d4d176490754391022c6e72993729c2992064dad6b167d898bd252e7bd3c2f，
fixture 91ad320d…，两候选 env unset）ON/OFF 均真实 rc=0、diff_count=0；
476 个共享文件 ON/OFF 逐位一致（非干扰负控成立）。
- writer_check ssm（36 线性层）：kernel 内 t==0 独立 S_smem 捕获（不经
  ckpt 写路径）== ckpt SSM row-0 逐位一致；writer_check conv（36）/
  ple（1）：宿主重建 [old_state[1..hist-1], 原始输入 row0] == ckpt
  row-0 逐位一致（同时确认 checkpoint 语义：row t = batch token t 之后
  的窗口）；restore_check ssm/conv/ple：ckpt row-0 == restore 后 live
  态逐位一致；restore_xcheck ssm：独立捕获 == restore 后 live 态逐位
  一致；restore_check kv（12 全注意力层）：restore 前后 KV 有效区
  [0..P=1050] 逐位一致（production restore 为 recurrent-only，未扰动
  有效分页区）；row0 argmax=13 val=23.375 与六格 cell1（step-26 batch
  侧）一致。
- 结论（限定）：step 26（真实分歧步）上 checkpoint writer/restore
  机制忠实——row-0 checkpoint 与 kernel 内部态（SSM）及原始输入重建
  （conv/PLE）逐位一致，production restore 精确恢复 row-0。六格中
  S_B1 vs S_T4 的 25 组件状态差不是 save/restore 损坏，而是 T 路径
  算术本身（T=4 batch 前向在 pos 1050 算出的 recurrent 态与 4 次顺序
  B1 不同）——与已知 T 路径残差（GEMM tiling/GEMV/mix-down 舍入差）
  同类。限制：S_smem 捕获独立于 ckpt 写路径但不独立于 kernel 算术；
  conv/PLE 重建依赖文档化滑窗语义（由逐位一致确认）；旧前缀 KV 参照
  为 T4 后 live cache（未独立重推）；host position/history 回滚与 KV
  失效由 MTP 流程调用方负责，本测试不覆盖。
- 过程记录：ON 曾在 binary 9a3875af 上以 sticky illegal memory access
  失败；两处修复（decoder_layer.cu + 测试 conv/PLE 宿主重建，均在未
  提交工作树）由 verify2（binary 16f50299，ON diff_count=37 全为
  conv/ple recon）/verify3（binary e7d4d176，ON/OFF 均 0）记录；本轮
  用当前 binary 独立重跑确认。证据 .q4t-work/mtp-writer-row0-20261011/
  （on_diag1/、off_diag1/、final_evidence_header.txt、两目录全量哈希
  清单）。本轮结束：不对齐 main、不 push/部署、不改 kernel 产品行为/
  阈值/性能；两候选保持默认关闭；checkpoint 机制排除后剩余根因=T 依赖
  算术（算子级已特征化），近 tie 分歧契约容差在 EVALUATION.md 仍未
  定义，由协调线程决策。详见 2026-10-11 日志与断点文件顶部。

**新一轮有界复现 + 六格诊断完成（2026-10-11 本轮，取代下方 in_proj_qkv
描述为最新状态；本轮已收口）**：三项测试均完成（binary
e95d560069a842e4d1de91cf27a857e5e06a471a697744a7652d5012f4517b76，模型
哈希 11:06 冻结 229 文件 err 空，两候选 env unset）；repro256/sixcell
独立 wrapper rc=0 已核实，roundtrip 为日志 PASS、真实进程 rc 未记录。
- mtp_fullstate_roundtrip：完整模型完整有效状态（host
  position/history/seq_id/stage + rope_delta/rope_pos + recurrent + 有效
  区 [0..P-1] page_table/KV slot/idx_raw/idx_comp）save/restore 逐位精确
  往返，日志 PASS（有限扰动/functional re-decode 核验；不能证明所有恢复
  机制无误，不能排除最初 checkpoint writer 写错行或分量）。
- mtp_repro_256（Part A）：首个分歧 step 26（pos=1050, input=55404）：
  plain=11 batch=13，gap_plain=0.0（完全 tie），gap_batch=0.125，
  l2_rel=0.0533445；fixture v1（fixture.txt）。非旧 step217（新基线）。
- mtp_sixcell（Part B）：S_B1（B1 replay）vs S_T4（prefill + B1
  replay[plen,P-1) + T4[last,42,42,42] + restore ckpt[0]）交叉核对 25 个
  组件差（recurrent first_off=0 + 每层末位 KV/idx_raw；host/history/
  rope/page_table/idx_comp 全 identical）；六格 row-0：cell0 argmax=11
  （23.25/23.25 tie→最低 id）、cell1/2 argmax=13（23.375 vs 23.25）、
  cell3 argmax=13（23.5 vs 23.25）、cell4/5 argmax=11（23.375/23.375
  tie→最低 id）；cell1==cell2、cell4==cell5 逐位一致（filler 独立）。
- 三类因果对照（离线，重跑逐字节一致）：state（cell3 vs cell0）
  l2_rel=0.055385、path（cell1 vs cell0）l2_rel=0.0543482、causality
  逐位一致。扩展：T4 续接下 state 对照 0.055851、S_T4 下 path 对照
  0.047751。
- 结论（限定）：本新 fixture 中初态与路径都能改变 greedy（path 对照单独
  复现 step-26 分歧；state 对照同样翻到 13，gap 0.25；S_T4+T4 下 tie 在
  23.375 重现→11）；两个预先固定后缀的 row0 逐位相同。S_T4 由不同 T4
  算术构造（含 checkpoint 保存/恢复），两初态差异不能直接归因于
  restore，也不能直接归因于 T 路径状态更新算术单独（两来源本轮未分离）。
  cell0/cell1 即真实 step-26 分歧的两侧（同一状态/输入/filler/
  save_checkpoints=1）。与旧 step217 事件同型（plain 侧完全 tie、batch
  侧破 tie）。仅测量，未用 0.02 验收；近 tie 分歧的契约容差在
  EVALUATION.md 仍未定义。
- 证据收口：evidence/ 102 文件（f_B1/f_T4 有效组件 + 元数据）sha256sum -c
  全 OK；env 清单确认两候选 env unset；binary/模型/fixture/退出码可互相关联
  （.q4t-work/mtp-repro-20261011/）。
- 本轮结束：不对齐 main、不 push/部署、不改 kernel/产品阈值/跑性能；候选
  保持默认关闭。独立审查正在据此选择一个最小恢复机制对照，待审查完成及新
  授权后才推进下一实验。详见 2026-10-11 日志与断点文件顶部。

**in_proj_qkv 单算子参考核验 + 唯一 QKV 局部候选（2026-10-11 本轮，
取代下方 cap8 描述为最新状态）**：首个分歧算子（L0 in_proj_qkv，同
输入 15/10240 差）完成单算子参考核验：Q4T_INPROJ_CAPTURE 采集 layer0
参考 T=8 的 x/weight/qkv_raw（cap8 ON，真实 EXIT=1，
/tmp/o2g_inproj_cap.log；该次运行于加 QKV 候选前的构建，binary
sha256 当时未记录——标未记录，不倒填 cd4ac50d；其后 cd4ac50d 的
OFF 负控日志与 o2g 日志 hash 相同、两轮 weight/x 采集逐位一致，
交叉核对成立），x row7 与 on4 probe x 逐位一致（同输入成立）、
qkv row7 与 on4 ref8 逐位一致（hook 与层 dump 一致）、权重与候选
ON 轮采集逐位相同。FP64 参考（BF16
解码操作数）：15/15 差异元素均更近 GEMV（T=1）侧（14×1-ULP +
1×5-ULP idx9835：ref 距 GEMV 0.347ulp、距 GEMM 5.347ulp；全行最近
BF16 ref==GEMV 10239/10240 vs ref==GEMM 10224/10240）——T=1 GEMV
为正确舍入值，T=8 cuBLASLt 在 bin 边界差 1–5 ULP，与 mix-down O2
同型。唯一 QKV 局部候选（src/model/linear_attention.cu，
Q4T_INPROJ_ROWWISE_GEMV，默认关闭、可回退；2<=T<=8 且无 FP8 shadow
且 hs%8==0 时逐行 Bf16Gev，仅 in_proj_qkv）：算子级复验同 x row7 下
T=8 qkv row7 与 probe 逐位一致（对齐成立）；最小 S7 on/off（cap8
保持 ON）：OFF 负控 a=0 .01684 / a=1 .01918 / a=2 .03300（与基线
逐位一致，日志 hash 相同）；ON a=0 .00871 / a=1 .01772 / a=2
.03471（均真实 EXIT=1）——混合效应：a=0/a=1 改善、a=2 纯路径差略升，
候选单独不能修复 S7；Part D 状态级仍无差。S8 超出本轮计划（边界指令前已启动跑完，
out-of-bound 参考：OFF .02534/.02692/.06625、ON .02050/.04271/
.06757），cap8/QKV S8 按协调口径标未测。候选保持默认关闭、不提升为
正式修复、不扩 z、不做第二候选；剩余 a=2 路径差（.03471）来自其它
T 依赖算子（in_proj_z 6 处暂不修等），未特征化，由协调线程下一小轮
决定。hash 清单 /tmp/q4t_inproj_cap_20261011/hash_manifest.txt。详见
2026-10-11 日志与断点文件顶部。

**mix-down 候选 cap8 实验（2026-10-11 本轮，取代下方 cap4 描述为最新状态）**：
Q4T_HC_MIX_ROWWISE_GEMV 候选 T 上限由 4 扩到 8（仍 env 门控、默认关闭、默认
行为不变）。同一已捕获输入算子对照（binary q4t_tests sha256 ced007de…，真实
EXIT=0，/tmp/o2e_cap8_op.log）：T=4 与 T=8 的 ON row0 均与 T=1 GEMV 逐位一致
（各 5/5），OFF 负控均与 Bf16Gemm M=4/M=8 逐位一致（各 5/5，旧行为保留），
残差为已知单 ULP 类（idx12 l2rel 7.599e-05，T=4/T=8 相同）。最小 S7：OFF 基线
a=0 .00977 / a=1 .01731 / a=2 .08069（与上一轮逐位一致，EXIT=1）；ON cap8
a=0 .01684 / a=1 .01918 / a=2 .03300（EXIT=1）——cap8 把 a=2 纯路径差由
.08069 降到 .03300（59%），证实 mix-down（含 T=7/8 prefill 路径）是重要来源；
但 a=2 仍超 0.02 阈值，剩余 .03300 为其它 T 依赖算子的纯路径差（Part D：
batch-final==pref7 全组件逐位一致、无状态差；Part E：batch-final==pref7
path-only .03300、seq T1x3 .07174、argmax 全一致）。表述区分：本轮 T4/T8 算子
均运行于 cap8 binary；cap4 的 S7 对照来自上一轮 /tmp/o2c_s7_cand_on.log，非本轮
同 binary 实验。候选保持默认关闭、可回退、不提升为正式修复、单独不能修复
S7/S8；不扩到其它算子。本轮用新输出目录（/tmp/q4t_cand_cap8_20261011_*）与
日志名（/tmp/o2e_*），上一轮证据完好、原始捕获只读复用。详见 2026-10-11 日志。

**算子级对照（2026-10-11 修订，取代“4 源统一修复”框架）**：默认
S7/S8 仍 EXIT=1，MTP 正确性总体未通过。旧 A→B→C 消融与 161 翻转
分解测于带 checkpoint stride 缺陷的轨迹，不作默认路径无缺陷或
因果归因证据；消融钩子下 0.00000 只说明该钩子在该测试配置下消除
翻转，不等于默认路径逐位一致。当前任务收窄为具体算子对照：固定
相同算子输入，检查首个 GEMV/GEMM 或 HC 差异及其累加/舍入行为；
证据充分后才做最小修复，暂不做 4 路统一改写。已完成 S7 状态级/
路径级/消融结果直接复用（见下），不重跑。断点：
`.q4t-work/mtp-correctness-contract.md`（顶部算子对照节）。
O0–O1 已完成、O2 最小修复进行中（2026-10-11，binary 46ebc519，
证据 /tmp/o0_*.log + /tmp/q4t_op_capture_s7_v2/，测试真实退出码
已用直接重定向核验：5 算子用例 EXIT=0、S7/S8 EXIT=1）。**撤回
（2026-10-11）**：①“GEMV vs GEMM(M=1) 逐位一致”不是跨算法对照
——Bf16Gemm(M=1, beta=0) 默认 dispatch Bf16Gev（GEMV，linear.h:
143-144），该对照实为 GEMV vs GEMV，不能称独立 cuBLASLt M1；
②“剩余 T 依赖源为 GDN 寄存器 kernel”撤回——S7 为 GdnRegOff 且
batch-final 与 pref7 预 probe 态逐位一致（A2/A3 证据），旧 161
翻转日志测于 stride 缺陷轨迹，不得据此改查 GDN。当前实际证据：
同一 mix-down 输入下 GEMV（M=1 SIMT，T=1 生产路径）vs cuBLASLt
M=4（T=4 verify row 0）至多 2/320 元素差、l2rel ≤ 7.6e-05、单
ULP（idx12 elem127：-7.5625 vs -7.59375 相邻 BF16，FP64 参考
-7.577701 非中点 -7.578125）；HC inject gate 生产算子 pre-gate/
post-gate 均逐位一致（仅覆盖真实 S7 4 次调用）。S7 仍 EXIT=1/
.08069、S8 仍失败。O2 进行中：test-only FP32 捕获（Bf16GevKernel
fp32_out，生产 nullptr、默认行为不变；Bf16GevFp32）+ 新测试
mix_down_fp32_s7，对已捕获 S7 mix-down 输入输出 GEMV 舍入前
FP32 累加、两侧 BF16 结果、FP64 参考与两侧 BF16 舍入 bin，把
差异特征化为累加顺序 vs 系统性偏移。**定位完成（2026-10-11，
/tmp/o2_fp32_run1.log，真实 EXIT=0）**：5 个差异元素
（idx12/13/14）全部满足 |GEMV FP32−FP64 参考| ≤ 9.2e-07，且
FP64 参考全部落在 GEMV 的 BF16 舍入 bin、全部不落在 cuBLASLt
bin——GEMV 为正确舍入值，cuBLASLt M=4 在 bin 边界元素差 1 ULP；
差异=累加顺序/舍入差，非系统性偏移、非算子缺陷。O2 方向待决策：
(A) verify 小 M 走 GEMV 逐行（对齐 GEMM 路径、保留基线，但
verify 权重流量 ×4、性能代价显著，且不含 GDN 状态路径）；或
(B) 按精度契约（EVALUATION.md：不要求所有路径逐位相同；差异须
有事先定义合同和证据）把 T 路径残差特征化为契约允许，并验证
用户可见 token 行为。O2 目标=保留既定精度、让
正常 MTP 正确的最小调整；不得仅开全局消融宣称修好，须相同已
捕获输入复验算子及 S7/S8 并保留旧行为负控；若主张路径差异可
接受须从原项目精度契约得出并验证用户可见 token 行为。旧消融
归因（GEMV 32 / GEMM_TILE 58 / HC 56）不被固定输入对照支持。
详见 2026-10-11 日志。

补测修订完成并 PASS（2026-10-11，binary q4t_tests sha256
bd5d6d6e1d33ca74c0ad5de0bbe4888e065e8043da29a8b6bd6382ea125124b2）：
mix-down 生产分支原样提取为共用 helper MixDownStep（生产与测试共用；
rmsnorm launch 失败现报告实际 CUDA 错误码）。补测
mix_down_candidate_s7 以已捕获 normed（RMSNorm 后生产输入）+ 已捕获
mix_down 权重直接驱动 MixDownStep——无再归一化、无整层 dummy 权重（旧
整层版对已捕获 normed 重跑 RMSNorm，同一算子输入主张无效）。相同已
捕获输入 on/off 对照（5 个 T=1 调用，真实 EXIT=0，/tmp/o2d_cand_op.
log）：候选 ON T=4 row0 与 T=1 GEMV 逐位一致（5/5）；候选 OFF 负控
与 Bf16Gemm M=4 逐位一致（5/5，旧行为保留）；输入交叉核对 10 次全逐
位一致。既有 6 算子用例全 RC=0；最小 S7（候选 OFF）打印关键指标与重构
前基线一致（l2_rel/argmax 行；a=2 .08069 MISMATCH，真实 EXIT=1）
——仅打印指标一致，S7 全输出/全状态 bit-identical 未验证。
候选保持默认关闭、可回退；S7/S8 on/off 混合效应不变（S7 a=0/a=1 升、
a=2 超阈值；S8 边界超阈值、full a=3 候选 ON 回退 .06625），候选单独
不能修复 S7/S8。O2 方向决策（A：row-wise GEMV 扩到其它 GEMM vs B：
契约接受，B 已否决）待协调线程/用户决策；下一小轮由协调线程决定。

S7/S8 可复用证据（2026-10-11，A0–A5；仅测试配置，非默认路径
结论）：S7 Part C 全接受（a=2，batch 末态、无 restore、decode
probe l2_rel=0.08069 > 0.02）2 层最小案例（binary sha256
0a71677189971b61e32cf8f8f7d052202f452ce5b4ae70887c367cebca42a47e，
证据 .q4t-work/mtp-fullaccept-isolate-20261011/）：
- 状态级（Part D）：batch 末态 vs prefill(7) 态，5 组件（L0
  ssm/conv、L1 ssm/conv/ple_conv）全部 diff_bytes=0 逐位一致——
  全接受无状态差；vs 顺序 T=1×3 态 relL2 2.9e-4–9.5e-3。
- 路径级（Part E）：同一 probe（position 7）从三态解码 vs
  prefill(8) row 7：batch-final 0.08069 / 纯路径差（restore
  prefill(7) 态）0.08069 / seq 态 0.06804；argmax=39535 三态均
  与参考一致——0.08069 为纯 decode(T=1)-vs-prefill(T=8) 路径差。
- 消融（A1，仅测试配置证据）：GEMM_TILE 单独 → 0.00000；group B
  （GEMV_T1+GEMM_TILE+HC_T1）→ 0.00000；HC_T1 单独无变化。
- S8 decode 边界（7 组消融）：baseline 0.02649/0.02146/0.02605；
  GEMM_TILE 单独 reject/partial → 0.00000、full 0.02448；
  GEMM_TILE+HC_T1 → 全 0.00000。

## MTP 正确性契约核验（2026-10-11，修订：已观察事实与未排除项）

本节初版（"有界结论"）超出证据，按 2026-10-11 审阅修订：结论只保留
已观察事实，未验证项明确标未排除。证据 binary fe80f004，
.q4t-work/mtp-contract-20261011/（256 步差分，1K 同 prompt）。

已观察事实：
1. **翻转（step 217，1/256）是近 tie 事件**。plain 侧 token 14 与 466
   在 BF16 下完全 tie（均 25.25），argmax 取最低索引 14；batch 侧
   token 466=25.375（高 0.125）打破 tie。l2_rel=0.069757，
   confident_both_sides=0。
   未排除：正式契约（EVALUATION.md）未定义近 tie 分歧的容差；"v1
   判据"是诊断判据而非项目契约，不能引为"契约允许"；若契约要求
   greedy 输出一致，本例不满足。
2. **状态差异：尺寸契约 + 有限采样**。快照尺寸与逐层 ssm+conv+
   ple_conv 期望总和一致（115642368B，读写契约有效）；末步采样
   （每分量类型 3 实例）全部 finite，reldiff ssm~1e-4–3e-4、conv
   ~4e-3–7e-3、ple_conv~6e-3–1e-2；差异步数 ssm/conv 256/256、
   layer1 ple_conv 251/256，自 step 0 起。
   未排除：全范围 maxabs/相对范数未测（初版日志声称已算，不成立）；
   "小数值差异、非缺陷"仅由采样实例支持；"非真实恢复错误"未证明
   （同数值路径 save/restore 往返核验待做，S7）。
3. **接受/拒绝：代码审查 + 2 层回归**。接受规则=与主模型逐位置
   argmax 连续匹配的最长 draft 前缀，correction=m_argmax[a]；
   accept=0（全拒）路径恢复 checkpoint[0]、仅发 [b]、
   next_b=m_argmax[0]（代码审查 MtpSpeculativeStep）；2 层模型
   checkpoint 回归 a=0/1 正验 PASS、负向 FAIL（可抓旧缺陷）。
   未排除：完整模型无最小复现；2 层回归不证明完整 MTP；强制
   accept 0/部分/全的输出口径未核验（S8）。
4. **RNG/采样：代码审查**。MTP draft/verify 与 serve 均纯 greedy
   （argmax；serve 默认 temperature=0，非 greedy 值被拒），无
   curand/采样。
   更正：两条路径各自确定，观察到的差异是确定性数值路径差异；
   初版"唯一非确定性来源是数值路径 T 依赖"表述不成立（不同确定性
   路径 ≠ 非确定性）；运行间可复现性未验证。
5. **多序列：2 层模型证据**。packed verify logits 逐位一致、accept
   recovery PASS、负向可抓缺陷。边界：完整模型多序列数值未单独
   验收。

**总体（final，2026-10-11 收口）**：MTP 正确性**未通过**。最小核验
S7/S8 已完成（binary sha256
1c73498393b0c10efeda9003a3fec0aa0c17dc67aba5063b4ca5741eb3b2f5aa，
证据 .q4t-work/mtp-contract-20261011/：run_s7_final.log sha
c9daaafb1dc93a84…、run_s8_final.log sha b0f95b15a11ae308…，均真实
EXIT=1 保留）：
- S7（model_verify_ckpt_samepath，2 层受控模型，GdnRegOff）：Part A
  恢复往返 a=0/1 逐位一致（6598656B）；Part B 保存数值惰性 + 同路径
  确定性逐位一致；Part C accept=0/1（restore 路径）l2_rel=0.00977/
  0.01731 < 0.02 OK，accept=2（全接受，batch 末态、无 restore）
  l2_rel=0.08069 超阈值——**原因已隔离**（2026-10-11 A2–A4：已知
  T 路径残差，状态逐位一致、纯路径差，见当前阶段节）。
- S8（mtp_spec_accept_states，2 层受控模型 + Q4T_MTP_TEST_DRAFTS
  钩子）：reject a=0 / partial a=1 / full a=3 的 accepted_count/
  accepted_tokens/next_b/position/history 语义全部正确；decode 边界
  l2_rel=0.02649/0.02146/0.02605 均略超 0.02——**原因已隔离**
  （2026-10-11：GEMM tiling/GEMV + HC inject gate，见当前阶段节）。
未排除项：契约容差定义（1）、全范围状态统计（2）、完整模型接受/拒绝
复现（3）、运行间可复现性（4）。全接受/decode 边界超阈值原因隔离
（S7 C / S8）已完成（2026-10-11）：均为已知 T 路径残差，非路径
缺陷，见当前阶段节。不改变默认部署（MTP 关闭），不部署服务。

## 当前结论
## 当前 Goal 已完成：MTP阶段性成果已合入main

2026-10-09纠正之前过早结束Goal的判断，已继续完成主线入口、
prefill停机准入、共享位置复制、GPU完成/失败状态、T4 stop与
输出尾部修复，并将foundation独有历史并入原research分支。
实现`74893ff`、历史合并`d621d00`沿用PR #3，未新建重复分支。
用户确认后，最终候选`5a3065b`于2026-10-09 16:36（北京时间）
通过[PR #3](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/3)
合入main，主线合并提交`ff5f495`；PR #2全部历史随之纳入。
当前[最终交付报告](MTP_MAINLINE_RESULT_2026-10-09.md)与
[最终证据包](evidence/mtp-mainline-final-20261009/README.md)
记录支持边界、实际成本和未覆盖项。合并树与获准候选完全相同，
交付记录仅改文档；原证据包保留封存时的未合入状态，不改写历史。

默认MTP关闭，显式`serve --mtp`选择sequential；指定Thor/checkpoint、
文本greedy、S1/slot0/k3、208896/8192的有限能力已进入主线。
快速T4继续实验，本轮恢复/终止修复不代表全链路数值准入。
不是所有模型任务质量通过，原8题中4项双方语义不足仍保留。

最终候选eae6949b：5 policy/3生产控制/18真实CUDA/54 Python通过；
19修复HTTP（1生产、18变体）及30生产五档均首次通过，15对跨模式
全文/计数/finish一致。初次wrapper链接失败及两处离线审计工具
误判均保留，修复后仅重验受影响构建/离线审计。独立成本822项
复核通过，5组服务正常退出0无残留，保护工作区与旧构建保持。
旧115请求与M1入口8条按原身份复用，未标成最终候选重新运行。

sequential五档decode为16.599/16.030/15.894/16.059/15.303 tok/s，
比同版本普通低10.057%–12.032%，整请求耗时增加4.472%–12.163%。
当前接受范围为可显式启用的正确性实现；没有提速/性能持平结论。
最终头公共检查通过，实际主线合入及回退点已记录，本阶段交付完成。
未自动部署或开启MTP。下一阶段可基于这个正确性参考单独制定
性能目标；快速T4数值准入、提速和整模型质量仍需各自证据。

## 上一阶段证据：基础修复可审，MTP sequential 有限正确性资格通过

按用户“先有阶段性可转正成果，再继续性能改善”的要求，
[转正Goal](MTP_RELEASE_PLAN_2026-10-08.md)已完成验收与交付整理。
A基础修复包`5aacc9f`及[PR #2](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/2)
在该阶段可独立审查、尚未合并；其证据复用范围不变，
不称新binary重新跑过HTTP。
基础分支的独有[报告与证据](MTP_FOUNDATION_2026-10-08.md)已并入
研究分支，保留原身份和两父历史；最终主线合入见本页顶部。

B新增显式`serve --mtp --mtp-verifier sequential`，指定Thor/checkpoint、
纯文本、greedy、S1/k3、208896/8192容量达到有限正确性profile准入。
实际生成草稿并接受/拒绝，target复用普通scheduler B1算术；默认
MTP仍关闭；该旧候选省略verifier时选择实验T4（M1已改为sequential）。泛能力日志仍将所有MTP
归为experimental，有限资格以参数、候选身份与实际verifier/path为准。
详见[验收结果](MTP_STRICT_RESULT_2026-10-09.md)与
[证据包](evidence/mtp-strict-20261009/README.md)。

候选a566faf5完成原110 HTTP及独立变体补件3+2，共115请求；其中
105条生产候选、10条变体分别记录。48对普通/strict全文/实际计数/
finish一致，取消/中途错误/资源回退恢复与同模式fresh一致。
原直接长前态与两模式末项边界共三次coverage FAIL均保留，缺失域
由固定长状态/真实尾部控制定向补件补足；未补有利采样。自然完整
GPU extend只观察1/4，2/3的生产TU控制和受控verify不能冒称全GPU
覆盖。持续8题有4题人工语义不足，不能称整体模型质量通过。

五档各三次均完成HTTP/输出检查；strict decode为16.609/16.061/
15.917/16.085/15.285 tok/s，较同候选普通低9.919%–12.256%，整请求
增加4.490%–12.214%。本阶段接受正确性参考，不接受提速/性能持平
结论，不使用旧T4速度代替。后续性能目标应基于此固定参考另定。

最终558文件、130运行源码、4受保护工作区与12执行组清理检查通过；
main、两个原dirty工作区、foundation与旧构建/证据保持，无遗留服务。
阶段代码与证据已在`863fc35`提交并推送，同一研究分支
`codex/mtp-admission-20261007`对应[审查草案PR #3](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/3)。
该PR包含此前未合并研究历史；优先独立审查A基础修复PR #2，
B按有限profile及成本单独审查。此处为当时的阶段收尾记录，整体完成判断已在本页顶部纠正；未合并/部署。

## 上一阶段完成：MTP批量GatherQuant通过冻结性能门禁

同一工作分支的首候选`dcff64bd`零警告，质量11/11、有限数值及
正式五档15条全部首次通过；对原`21d85a17`严格15/15格通过，
primary未重置。五档decode为29.7484/30.0018/26.6740/22.0605/
27.5486 tok/s，对primary提高3.580%–4.677%，对辅助C2提高
2.288%–2.689%；整请求均缩短。**1K全三次TTFT均值仍增加0.126%，
其后两次均低于原上限**，不能称全档TTFT均值更快。历史固定顺序
各三次不证明因果显著性，详见[候选结果](MTP_BATCH_GATHER_RESULT_2026-10-08.md)。

[主验证MoE诊断](MTP_VERIFY_MOE_2026-10-08.md)显示旧verify占循环
约81%–82%；全量平均28.77个活跃专家，固定样本仅占0.426%，
不能外推全层时间。候选按原四stream批量GatherQuant，保留真实
M_e、独立SF、原GEMM与依赖，增加1,024,000B临时预算。正式组
实际56,304次应用，将1,619,634次该类launch降为225,216次，
无bad-counts回退；没有将提交减少比例当耗时收益。候选内部计时
关闭，草稿/验证新绝对成本未测，不从旧样本推断分段改善。

10组host、三组预算、三路由六次正常加一次失败清理、短17步
123raw和长10步90raw首次通过。长分支batch off/on的recurrent
与MTP缓存内存exact通过；历史raw不含完整跨版本缓存，main完整
KV、中间draft-head logits及普通/MTP跨形状等价不在该结论内。
本Goal共67条新HTTP，测量进程自然退出，无额外探针或重采样。

本阶段以一个候选收尾，第二个并非必须。跨专家SwiGLU batch会扩大
GU保留和提交顺序变更，且缺候选开启后分段证据，本轮未实施。
候选独立原始审计首次通过805,587项检查与1,320项来源绑定，
最终保护932项全部通过，原两个dirty工作区、main与旧构建保持。
本阶段无待运行任务，后续如继续，先冻结batch路径真实分段计时，
再决定剩余验证或草稿方向。历史性能NO_GO和跨模式启用NO_GO保留，
MTP/batch默认关闭，同一分支保留，未合并或部署。

## 上一阶段完成：MTP整体有小幅收益，严格性能未准入

沿用`codex/mtp-admission-20261007`完成有界Goal：完整循环成本账、
两项候选及共93条实际HTTP，固定质量和输出/路径检查全部通过。
原始循环、候选数值与两轮五档均已封存，不追加第三候选或有利采样。

最终组合`962b5632`相对原MTP基线`21d85a17`：五档decode提高
1.26%–1.94%，整请求均缩短；44K/200K平均TTFT减少0.261/1.161秒。
严格15格中14格通过，仅1K一次TTFT超冻结上限2.002毫秒，出口
**NO_GO_FOR_CANDIDATE_PERFORMANCE_ACCEPTANCE**。这不构成已证明
的因果回退，也不允许临时放宽门槛。第一候选单独的4格失败保留。

实现分别减少B1草稿/追赶的重复位置读回，以及省略长初始化非末
分块的未使用尾部；保留attention/KV/indexer和末块计算。最终质量
11/11、28parser、新11/40/29分块覆盖、五形状30forward及8196
长初始化后Full/Skip共10步exact首次通过；独立raw45复核一致。
有限证据不能扩大为普通/MTP跨形状等价，默认MTP仍关闭。

主验证仍占循环80%–81%，本轮没有加速其计算主体。下一优先级是
T4 MoE路由/分组的实际成本，再考虑GPU grouped调度与接受率。
约0.4%的线性临时分配不优先；counts等待包含前序GPU，不能直接
当纯同步成本删除。未来另冻结有界协议，本Goal无待运行任务。

原两个dirty工作区、main、默认/历史构建和128运行来源共153项
保护检查通过；测量服务/runner/采样器全部退出，未合并或部署。
详见[完整成本与后续方向](MTP_OVERALL_2026-10-08.md)、
[第一候选结果](MTP_KNOWN_POSITION_2026-10-08.md)与
[最终组合结果](MTP_INIT_TAIL_2026-10-08.md)。

## 上一阶段完成：MTP 初始化诊断收尾，收益空间有界

沿用 `codex/mtp-admission-20261007` 完成默认关闭的计时工具和固定
41 条新 HTTP：质量 11/11、控制/诊断各五档三次全部首次通过，
实际 MTP、正式容量、输出及步数保持；15 条有效 trace 覆盖与闭合
通过。独立 10,632 项来源/原始记录检查及 188 项派生核对通过。
本阶段出口 **ATTRIBUTION_COMPLETE**，不改变既有性能/启用 NO_GO。

200K 主 prefill 159.254 秒，占服务首内容前 host 时间 95.284%；
MTP 初始化 7.532 秒，其中 draft attention stream 区间 5.468 秒。
即便理想删除整个测量初始化窗口，该 host 时间也只降低 4.507%；
这不是可实现的客户端收益。head 约 5.3 毫秒，临时分配约 3.6 毫秒，
不再是主要方向。开关计时的五档 TTFT 变化 −0.170% 至 +0.161%，
固定顺序三次采样不能证明零开销或严格持平。

下一步仅建议有界验证“初始化非末分块省略 mlp_hc/MoE/mixer”：
44K/200K 对应 stream 区间约 0.330/1.545 秒，不能承诺等量加速；
保留所有 attention/KV/indexer 更新、末块完整形状和后续 k3 合同。
本轮没有实施该候选；整模型跨模式数值缺口仍在，默认 MTP 关闭。
详见[最终诊断报告](MTP_INIT_TIMING_2026-10-08.md)。

同一分支保留实现与证据，未合并/部署。126 个运行文件、原两个
dirty 工作区、main 及默认/旧二进制核对无变化；测量服务、runner
和采样器已正常退出，本阶段无待跑任务，可以继续其他主线工作。

## 上一阶段完成：MTP 末行初始化节省缓冲，性能未准入

沿用 `codex/mtp-admission-20261007` 完成有界 Goal，出口
**NO_GO_FOR_PERFORMANCE_ACCEPTANCE**。唯一候选 `73c39bab` 只缩减
HTTP 初始 draft 的末行投影和临时 logits：8192 行末块从 3.789 GiB
降为 0.474 MiB；上游 hidden/KV、scratch 和保守预算保持原合同。
HTTP 第一测质量 11/11、六形状结构/有限数值包络、完整 k3 17 次
回放首次通过。默认 MTP 关闭，实验实现未合并或部署。

新 MTP 五档各三次全部完成，fallback=none，文字与旧 MTP 全部相同。
TTFT 比旧 MTP 缩短 1.28%/3.40%/3.88%/0.45%/0.34%；decode 均值
低 0.18%–0.59%，1K 整请求 +0.16%，其余缩短 0.16%–1.30%。
五格不满足预先冻结的后两次范围筛查，不声明严格持平，也不将
历史各三次的小差异解释成已证实的因果回退。没有追加有利采样。

相对历史普通模式，新 MTP decode 仍提升 18%–61%，但 TTFT 全档
增加，200K 整请求 174.09→176.53 秒（+1.40%）；跨模式数值缺口
仍在。短质量与有限包络不能外推整模型等价，资源采样也未证明
整机物理 RAM 上限。见[本阶段报告](MTP_INIT_LAST_ROW_2026-10-08.md)。

测试进程已退出；123 个运行文件身份相符，原两个 dirty 工作区、
main 与默认/旧受测二进制未变。候选与证据留在同一研究分支，
本阶段无待运行任务。后续如继续，先为剩余长上下文初始化成本
与跨模式数值边界另定有界目标；当前可继续其他主线任务。

## 上一阶段完成：MTP 有 decode 收益，仍不默认启用

沿用 `codex/mtp-admission-20261007` 完成有界 Goal，出口仍为
**NO_GO_FOR_ENABLEMENT**。本轮只新增四项直接诊断及证据，服务
`7fe34d6`、模型库和 123 个运行源码身份均不变，默认 MTP 关闭。
局部投影差异符合预定义舍入包络；T1 入口与 MoE 对照通过。完整
48 层 k3 的 16 自然加 1 强制步骤首次通过，实际接受 0/1/2/3 draft
全覆盖，17 次同形 logits/trunk/状态回放逐位一致。没有整模型
跨形状误差上界，旧跨模式逐位失败保留，不能称 plain 轨迹等价。

新增 MTP-on 五档各三次 HTTP 全部通过，复用同二进制普通基线：
decode 为 28.41/28.88/25.54/21.20/26.59 tok/s，相对普通提升
53.1%/61.4%/40.8%/18.7%/55.7%。TTFT 全部增加；200K 整请求
174.09→177.04 秒（+1.69%），因此未达到各档无回退条件。
五档跨模式文字均不同，这是固定同 prompt、等输出长度的实验
比较，且普通基线测于较早时段。HTTP 固定质量 off/on 各 11/11、
生成边界与上限按既有身份复用，不代表长文本质量等价。

资源观察、全部三次计时与限制见[阶段报告](MTP_CONTINUATION_2026-10-07.md)。
runner、服务、采样器已正常退出；原两个 dirty 工作区、main 和
默认二进制未变。该阶段提出的初始 draft-extend 末行优化已在
上文新阶段完成验证；这里的旧结果保留，不代表新阶段性能接受。

## 上一阶段结论：MTP 暂不准入，已完成有界收尾

2026-10-07 Goal 按预设 **NO_GO_FOR_ENABLEMENT** 出口完成。唯一工作
分支 `codex/mtp-admission-20261007`，实现提交 f02e75e / 0284b60 /
ec0ae6f 已推送。资源预算/失败清理、普通 decode RoPE、checkpoint
实际布局、生成尾部/回退已修复；默认 MTP 关闭，未合并或部署。

最终候选 `7fe34d6` 零警告构建。22 预算、2 策略、18 runner 合同、
CUDA 生命周期与两项独立数值缺陷回归通过。普通/MTP 固定 HTTP 质量
各 11/11；边界 12/12、输出上限 off/on 各 6/6。首次 MTP 预算缩容
失败保留；修正 index 声明总量误用后保持 208896/8192/S1 正式容量。

仍未闭合：四层直接对照及完整模型 1K/256 HTTP 均有普通/MTP 生成
分叉。取消协议和槽位通过；唯一新进程 MTP 控制与取消后恢复文本相同，
该输入未观察到取消残留。不能把跨模式差异直接称算法错误，也不能
据短答案质量通过宣布准入。MTP 五档收益未验收，不宣称加速。

普通新旧各完成五档 15 条同输入请求，各版本内部确定，新旧文本均变。
新 decode 为 18.55/17.89/18.14/17.86/17.07 tok/s，变化约 −0.31%
至 +0.18%，只作修复后的描述性对照，不声明严格持平。整体物理 RAM
上限未知。原 7 项/2 项修改、main 与默认二进制未变；测试服务已停止。
范围、失败、复用与证据见 [MTP 准入收尾](MTP_ADMISSION_2026-10-07.md)。

## 上一阶段：Offload 研究收尾，主线交接就绪

从真实 main 8ea85b1 建立独立工作区，收尾三个包：独立研究工具与
结论索引、WeightLoader 生命周期/并发修复、全驻留预算边界修复。
范围、来源与交接位置见 [主线收尾](MAIN_WRAPUP_2026-10-07.md)。Thor
Release 零警告构建、I/O 六项（含 ASan/UBSan）、预算十四项、四类实际
入口拒绝及 HTTP 固定质量 11/11 通过，含 200K。公共 host 18 组、331 项
合同完成；首轮唯一夹具路径失败已定向修复，首次失败保留，GitHub CI
通过。二进制 `0f52e926`；运行源码 `e6b4bc0`，后续仅工具夹具和记录修订。

完整 MoEResidency 与所有 NO_GO 优化留在研究分支；整体物理 RAM
54 GB 仍未证实。本轮不声明性能持平或加速，不覆盖原 build/q4t，
不包含原开发目录七项未提交修改。MTP 的完整预算仍有明确缺口，未纳入
本轮接受范围。服务已停止；此阶段告一段落，可在独立 main 工作区推进
其他主线任务，无需继续 offload 实验。

存储准备已完成：2026-10-07 清理 102 个停用的 C/C++ 编辑器索引缓存，
释放约 10.04 GiB，清理后可用约 10.45 GiB。原始证据、模型、构建环境、
原七项修改及两个 q4t 二进制身份均保留；运行源码未变，无需重复推理。
可继续常规开发与构建；大型采集按预计输出量另做容量预算。详情见
[存储交接](STORAGE_READY_2026-10-07.md)。

分支入口已整理：本地从 18 个收敛到 4 个，保留 main、最新 offload
研究入口，以及原目录和 wt-c1 两个有未提交修改的分支。其余 14 个
本地分支的提交均可由 main 或研究入口追溯；10 个旧工作区停留在原
提交的 detached HEAD，15 个工作区目录及证据均保留。原目录 7 项、
wt-c1 2 项修改和两个 q4t 摘要未变；远端研究分支未删除。后续同一
阶段以提交划分任务，减少重复分支。详见[分支整理](MAIN_WRAPUP_2026-10-07.md#本地分支整理2026-10-07)。

## 2026-09-28 部署与验收历史快照

以下默认部署描述属于 2026-09-28 的封存记录，不代表本轮已部署：
MTP 输出与 plain greedy 不逐位一致：真实 T 依赖回归，且为多源叠加
（2026-10-11 消融修订）。MTP k=3 基线（2026-10-10）：五档 MTP on
（binary c29d3340/696d8f1）全过；MTP off 以 on 组为参考交叉核对在
1K 档失败。差分测试（mtp_verify_numerics，1K 同 prompt，256 步）：
因果守卫通过（verify row 0 不依赖后续 batch token），161/256 位置
argmax 翻转（63%）、157 个两侧都自信（gap>0.05 双侧），max
l2_rel=1.056——非近 tie 噪声。09-19 MTP 逐位一致（QSA decode 优化
前），属回归。

消融实验（2026-10-11，同工作树 binary 逐步对齐 T=1 路径；基线数字
与 10-10 c29d3340 完全一致，佐证默认路径未变）：
- 稀疏注意力对齐（Q4T_ABLATE_QSA_T1）：零变化——QSA 稀疏注意力确认
  零贡献，10-10 的 QSA 根因定位被数据否定。
- GDN 寄存器 kernel 关闭（Q4T_GDN_REG=0）：161→146 翻转、157→135
  自信（15 翻转）——GatedDeltaNetRegKernel（仅 ssm_ckpt==nullptr
  启用：T=1 走、T=4 verify 退回逐 token 串行 kernel）数值不等价。
- 再加 GEMV 对齐（Q4T_ABLATE_GEMV_T1，M=1 专用 GEMV vs cuBLASLt
  GEMM）：146→114 翻转、135→106 自信（32 翻转）——GEMV 是另一来源。
- 再加 Bf16Gemm tiling 对齐（Q4T_ABLATE_GEMM_TILE，M=1 pad 到 M=4
  走同一 cuBLASLt 算法族）：114→56 翻转、106→52 自信（58 翻转，
  max l2 0.888）——cuBLASLt 对 M=1 与 M=4 选不同算法，是第三大来源；
  叠加 QSA 消融仍 56/52，QSA 零贡献再确认。
- A→B→C 收尾（2026-10-11）：A=GDN_REG=0+GEMV_T1+GEMM_TILE（含
  Fp4Gemm tiling 扩展）仍 56/52、max l2 0.888——Fp4Gemm tiling 零
  贡献；B=A+HC_T1 后 flips 56→0、双侧自信 52→0、max l2 0.000000
  ——HC inject gate 融合路径是最后一个翻转源；C=B+MOE_DECODE_T1 与
  B 相同——MoE M=1 device decode 零贡献。全部 161 翻转归因：GDN
  寄存器 kernel（15）+ M=1 GEMV（32）+ Bf16Gemm tiling（58）+ HC
  inject gate（56）= 161/161，消融可归零。代码核查已排除 indexer
  （单序列 T=1/T>1 同走 tensor-core Bf16Gemm+IndexerReduceKernel）
  与 conv（单序列同 kernel）。
- 状态分歧已定位并修复（2026-10-11）：双源——(1) checkpoint 保存/恢复
  stride 契约错位（RunLayers 按 num_ckpt 保存、恢复/直读按 cap；统一按
  cap，含三个多序列 kernel 的 seq stride）；(2) 单序列 verify 的 PLE conv
  checkpoint 缺口（新增 PleConvCheckpointSingleSeqKernel，且
  ModelDecodeBatch 原先漏传 d_verify_ple_conv_ckpt，生产单流 MTP verify
  同路径，已补）。修复后 group B 与默认配置 256 步：checkpoint 直读
  与 T=1 状态在 group B 下 0 差异；默认配置下状态仍 256/256 步差异
  （max≈84MB）——该残留是状态更新计算本身的 T 依赖（GDN 寄存器 kernel
  vs 串行 kernel 等，即翻转同源），属阶段 2 统一修复范围，非
  checkpoint 保存/恢复缺陷。
- 翻转计数口径标记（2026-10-11）：修复前基线 161/256 翻转（max
  l2_rel=1.056）测于 stride bug 下（reserve=4 时保存 stride=3、恢复
  stride=4，每步恢复错层/错 token 状态）；修复后同测试默认配置仅
  1/256 近 tie 翻转（max_flip_l2=0.069757）。旧翻转计数可能受错误
  checkpoint 污染，既有消融归因（15/32/58/56）需复核，暂不作为
  默认路径 T 依赖幅度的定论。max_flip_l2 仅指翻转位置的 logit
  l2_rel，不代表全部 logit 逐位一致。

修订后的计划（2026-10-11，四阶段）——**历史，不作当前行动依据**
（2026-10-11 修订：当前任务收窄为算子级对照，见“当前阶段”节；逐位一致确认前暂停 MTP 性能阶段分解与定向优化）：
1. 定位全部 T 依赖源：已完成。翻转部分——A→B→C 消融把 161/161 翻转
   全部归因并归零（GDN 寄存器 kernel 15、M=1 GEMV 32、Bf16Gemm
   tiling 58、HC inject gate 56；Fp4Gemm tiling 与 MoE M=1 device
   decode 零贡献；QSA/indexer/conv 已排除）；计数口径见上条标记。
   状态部分——checkpoint 保存/恢复路径的 T 依赖源已定位并修复
   （stride 契约 + 单序列 PLE conv checkpoint + 指针传递）；候选
   钩子均 env 门控、默认关闭、默认行为不变，ProjGemm FP8 路径仅在
   Q4T_FP8_PROJ 实验开关启用时激活（评估契约禁用，不在当前差分
   测试配置内）。离线 a/beta 比较（旧 groupB dump）72/72 逐位一致，
   支持修复前状态分歧来自 checkpoint 保存/恢复而非 GEMM。
2. 统一修复：使 verify（T=k+1）与 greedy（T=1）数值等价（含
   recurrent 状态），保留批处理带宽收益（不整体回退逐 token）；
   消融钩子收敛为正式修复或移除。前置澄清：已完成（2026-10-11，
   两者均为已知 T 路径残差，非路径缺陷，见当前阶段节）。
3. 重验：差分测试 0 双侧自信翻转且 recurrent 状态 T 独立；k=3 基线
   五档 MTP on/off 逐位一致；五档 E2E 性能无回退。
4. 恢复 MTP 性能阶段：Q4T_MTP_TIMING 阶段分解 + 定向优化（计划 B：
   draft 步 QSA top-k 索引复用，见 REFERENCE_MTP.md）。
验证（2026-10-11，修复后同 binary）：group B reserve=4/3 steps=1
状态/直读/flips 全 0；256 步 RUN1（group B，reserve=4）flips 0/256、
状态 0/256、直读 0/256；RUN2（默认，reserve=4）flips 1/256（近
tie）、状态 256/256（max≈84MB，属阶段 2 范围）。详见 2026-10-11
日志与 /tmp/mtp_256_runs.log。Checkpoint 回归测试（2026-10-11，2 层模型）：单序列 model_verify_ckpt（cap=4>T-1=2，a=0/1 恢复）与多序列 model_verify_multi_ckpt（cap=4>T-1=2、seq_ids {1,2}、a=0/1 恢复）正验 PASS（恢复后 decode l2_rel 0.00977/0.01731<0.02）；负向验证（回退旧 stride/旧 PLE 空指针）FAIL——测试可抓住两个旧缺陷。多序列测试初稿顺序 bug（re-prefill 先于 decode 覆盖已恢复状态）已修正。证据 .q4t-work/ckpt-regression-20261011/。

Phase D（router 权重优先驱逐）代码已按用户批准删除（2026-10-09）：
定向终判确认 decode 回退（204800 −5.6%、261887 −13.6% 跌破 50% 门槛），
纯 tick-LRU 为已验收行为，删除后与 Phase D 前 bit-identical。重建零警告、
单测 123/123（验收环境）。3b pread 合并统计接线已随 6e5befd 提交。MTP T 依赖统一修复（四阶段计划之阶段 2）不再是当前阶段（2026-10-11 修订：当前为算子级对照，见“当前阶段”节）。

## Offload分区运行时：完整性能NO_GO（2026-10-04）

详见[分区运行时验证](OFFLOAD_PARTITION_RUNTIME_2026-10-03.md)。
min_new_csr_v1已接入，默认关闭、严格开关、超预算整forward回退；
chunk_order仍0，GEMM/精度冻结，MTP及Phase D关闭。干净源码26ca438
构建7ce25d74零警告，首测HTTP固定11题、133项host/工具及两项真实
权重数值合同首次通过。BF16和所采FP32逐位相同，覆盖singleton/
非连续回填；只证明冻结层/形状，不外推全部中间值或全部任务质量。

45K初筛通过后，16GiB同binary完整六档off/on各3次于10月3日23:47收尾。
36请求输出/容量/身份及清理通过；48层9504次候选prefill、零回退。
全部TTFT门槛通过，但1K/4K/8K最低decode分别下降3.63%/0.45%/
0.076%，完整性能有效NO_GO。45K/200K/261887三档通过；长档平均
TTFT减少28.81%/31.20%/31.11%，不能抵消短档失败。按冻结出口拒绝
当前全范围策略，业务/生命周期条件未触发，无有利重跑或门槛放宽。

四组资源与42条请求日志首次审计完成。长档后两次GPU补载约减半，
200K/261887的PID存储读量三次均值却增加3.29%/3.54%；软件nvme_mb
不能替代物理读取。cgroup I/O低计并平台，livecache/PSI未知。
完整off/on内存计费峰比16GiB设置高729088/765952B，OOM/swap为0；
NVIDIA计数峰56.237GB不可当去重物理并集，54GB继续INDETERMINATE。
原7处修改独立保留；候选保持默认off，下一步优先短档阶段归属与缓存
状态诊断、整体RAM去重测量，不直接启用或改GEMM。

## Offload有界实验收敛：重排NO_GO，分区离线GO（2026-10-03）

详见[本轮完整结果](OFFLOAD_AUTONOMOUS_2026-10-03.md)。干净binary
6ab2a820固定质量11/11通过；16GiB同binary off/on六档各3次全部
输出/容量/身份通过且正常收尾。重排在1K、4K、261887档有冻结
速度门槛未过，完整性能NO_GO；长档平均TTFT改善约3.3%–3.8%，
不能抵消未通过项。Q4T_MOE_CHUNK_ORDER默认0，真实D验证未触发。

唯一后备min_new_csr_v1按冻结范围完成一次96层离线研究：两个
首8192-token forward的GPU补载减少48.49%/47.25%，结构下界
下降、块数下降、无回退。仅OFFLINE_GO_FOR_CONSIDERATION，
这是接入前的离线结论；随后真实路径验证已完成，见上节。单token
子块会改变dispatch，离线结果本身不提供数值/HTTP/SSD/TTFT接受。

133项host/工具检查通过，构建零警告；C++行宽整理后两个helper
二进制与实验版本逐字节相同，复用已通过证据，无重复模型/trace。
四组原始资源审计完成，42请求可夹逼计数；cgroup I/O低计并平台，
PID/分区读量相互支持但归属分列。完整off/on的memory.peak比
16GiB设置高864,256/880,640B，OOM/swap为0。NVIDIA峰56.237GB
不是整体物理并集，livecache/PSI未知；54GB仍INDETERMINATE。

原7处未提交修改独立保留，模型/reference只读，GEMM冻结、MTP与
运行时Phase D关闭；最终证据/工作分支交付身份见本轮报告。

## Offload离线回放 Goal完成（2026-10-02 22:31）

结果见[实际时序回放](OFFLOAD_REPLAY_2026-10-02.md)。58项工具合同通过，
8组冻结比较完成；运行时/binary46b3f977不变，已有HTTP证据复用。
当前45K真实日志的原分块顺序下至少382732次prefill补载不可避免，
单改驱逐的可避免上限仅7.0%–7.2%；固定各forward入口的局部理想
驱逐仅再少0.38%–0.40%，后者不能当全请求最优。

唯一重排候选在主45K及业务留出减少5.9%–7.0% GPU补载，decode计数
未增；主样本baseline前两次GPU计数与真实日志精确一致。L2/mirror
仍是条件排程；理想对象缓存缺失量改善仅0.2%–1.5%，不预测SSD/速度。
热点逐ID可由32条calibration重建，业务留出独立；旧chat holdout
已被后来校准集使用，不能再称当前热点留出。

建议保留重排作下一阶段唯一有界HTTP小步候选，不据此承诺TTFT−30%；
更大收益需研究分区本身。54GB整体RAM仍未通过，16GiB只为研究条件。
GEMM冻结、Phase D off，原7处未提交内容完整保留。

## Offload离线回放 Goal启动（2026-10-02 22:06）

范围见[实际时序回放](OFFLOAD_REPLAY_2026-10-02.md)。保持线上binary
46b3f977及GEMM/Phase D状态不变，复用已通过的HTTP；补逐token、
真实分块与分层缓存条件回放，冻结一个子块重排候选与固定分块理想值。
54GB整体目标未通过，16GiB仅作host/cache研究条件。已有trace缺少
worker/event时序，L2/mirror显式列模拟假设；不冒称物理IO或性能验收。

## RAM/缓存 Goal：预算修复与有界对照完成（2026-10-02 20:40）

完整结果与后续建议见[RAM/缓存协议](OFFLOAD_RAM_PROTOCOL_2026-10-02.md)。
新工作区binary `46b3f977`零警告；budget host/sanitizer各13/13，
工具45/45、原验收工具30/30。1%预算启动在model_load前拒绝；固定
HTTP质量11/11（含200K），两组45056×3输出均同参考，容量262144不缩减。

不限host/cache组TTFT 135.8/131.8/131.6s、decode9.82–9.97；16GiB
组223.1/224.1/224.3s、7.25–7.31。PID存储读取分别27.906/0.769/
0.174GB和438.850/441.757/441.614GB；逻辑读取均约1.14TB/请求。
这是局部资源对照，未接受性能策略、未跑完整五档+目标档矩阵。

冷态不限组memcg峰70.153GB；16GiB组峰17,179,885,568B（比设置高
16KiB），两组swap/OOM均0。CUDA未完整计入cg，整体物理RAM仍未知，
54GB目标未签PASS。受限cg io.stat严重低计（第三发仅0.223GB），
PID与分区设备读量相互支持，具体内核归属原因UNKNOWN；三口径分列。

三服务正常退出、unit清理、模型文件与binary身份均核实。原Phase D/
统计/日志未提交内容继续保留，工作区HTTP不等于干净提交树发布资格。
下一步先定部署预算，按真实分层/分块时序离线评估prefill重复补载收益
上界，再选一项在线策略；GEMM冻结、Phase D off。

## RAM/缓存协议阶段：隔离校准完成，预算待冻结（2026-10-02 19:20）

用户已允许启动下一阶段，计划见
[RAM预算与缓存协议](OFFLOAD_RAM_PROTOCOL_2026-10-02.md)。本轮只做
14阶段×3的小探针，无模型推理、无运行时代码修改、无模型cache清理。
独立1GiB transient服务内，CUDA device增加64MiB时memcg current
中位增量为0，而NVIDIA增加64MiB、KReclaimable减少同量；pinned和
普通匿名页进入计费。外部预热32MiB cache的读取也不新增file计费。
因此memory.max不能直接当q4t全部物理RAM上限。

探针exit0、无OOM，服务/cgroup/任务文件已清理。下一步分别核设备/
主机/cache覆盖，补有界HTTP与实际存储IO采样，处理预算无解仍回退
启动、空热点层计费规则漂移等已发现边界，再冻结小规模HTTP协议。
建议先探索C=256可运行资源范围；54GB是否为最终目标待选择，不
自行恢复68.34GB旧口径或宣称新的总物理峰值。GEMM冻结、Phase D off。

## Offload 基础修复 Goal：修复与有界验证完成（2026-10-02）

交付、身份与边界见 [修复报告](MOE_OFFLOAD_REPAIR_2026-10-02.md)。
3b关闭路径、部分短读、分片读期所有权已修复；完整工作区binary
`dc845dd3` 构建零警告，相关集成11/11、固定HTTP质量11/11（含200K）、
merge关闭1K/8K、故障及prefill/decode取消恢复全过。IO host与
ASan/UBSan各7/7；验收工具30/30、内存工具9/9。

逐文件账已核实：三组模型文件cache窗口峰73.58–73.95GB，NVIDIA
计数峰56.24GB、RSS峰约4.70GB；这些分项峰值不可相加。总物理峰值
仍为INDETERMINATE，旧68.34GB不得当作含缓存总峰值。两组未采到
退出端点及4次GPU查询空值原样保留；服务退出码均0。canonical工具
已修监控收尾竞争，并独立覆盖C3预热probe/矩阵进程。

阶段提交包含修复所必需的入口3b管线；Phase D及原统计/日志改动
保留在工作区。隔离3b提交候选独立编译零警告、residency8/8，
其源码身份与含关闭Phase D的HTTP工作区不同，不冒称同一发布身份。
本轮未重做性能矩阵，不宣称60% decode、TTFT降低30%或性能保持
达标。GEMM冻结、Phase D off；下一步先定实际RAM/缓存协议，再选优化。
下方均为历史阶段快照，当前结论以上述记录为准。

## Offload 诊断纠正：新候选合同缺陷与内存测量缺口（2026-10-02 16:47）

全面审查见 [offload 诊断](MOE_OFFLOAD_DIAGNOSIS_2026-10-02.md)。本轮
不改运行时代码，不启动模型测试；在途 d0 保持运行。以下纠正优先于
下方历史阶段说明：

- ed68cd3d 的六档 53.0%–58.1% decode、目标容量与所测 HTTP 输出
  一致性证据保留；当前 e5b7c782/未提交 3b+Phase D 不是该已验收身份。
- **68.34 GB 不能认定为含模型页缓存的总物理峰值**：门禁只加
  post-matrix−post-load 的 7.286 GB，漏掉加载期约 59.06 GB 的全机
  Cached 增长，并使用服务退出后的末态。准确总峰值须核归属、重叠
  与同时点；用户接受 C=256 超支的授权保留，数值口径待纠正。
- 新 3b 的关闭开关错误构造任务索引（host 最小复现已确认），
  ReadRangev 的部分短读重试未前移目标缓冲；另有分片缓存驱逐时的
  读期所有权风险。均未修复，不能把 3b=0 宣称为安全回退。
- 方法纠正：约 70% 相邻 overlap 不能证明 LRU 是重读主因；
  17.28 倍来自 45K 预热，目标档约 117.6 倍；nvme_mb 含页缓存命中。
  Phase D 按 forward 累加整批权重，与 token 时间尺度不同。
  capacity_curve 的留出 static/hybrid 使用本 split 选热点，不能
  作为部署预测；实际热点生成仅用 calibration 的证据仍成立。
- 建议下一步：成组修合同和验收脚本 → 核清内存/流量 → 复现真实
  分区与跨阶段缓存的离线模拟 → 选一个有收益上界的 prefill 改动。
  当前镜像写回的低复用值得优先检验；Phase D 维持 off、GEMM 冻结。

## Phase D 定向终判：decode 回退确认，默认回退 off；3b-only 定向在途（2026-10-02 15:35）

Phase D（项 4）定向干净重跑完成（binary 456e2117，3b+Phase D on，
204800×3 + 261887×3，C3 协议，无并发干扰），vs ed68cd3d 基线
（3b off/Phase D off，final-acceptance 矩阵，hmean）：

- 204800：TTFT 690.00 s（−4.1% vs 719.63）；decode 9.4344 tps
  （−5.6% vs 9.9901）；重读因子 90.98（vs 90.82，不变）
- 261887：TTFT 900.48 s（−3.9% vs 937.01）；decode 7.9751 tps
  （−13.6% vs 9.2313；=46.9% C=0 基线，低于 50% 门槛）；重读因子
  118.0（vs 117.63，不变）
- 机制：router 权重 EMA 被 prefill 观测主导，逐出 decode 实际需要的
  专家 → decode GPU miss ×3.4（204800 18.4k vs 5.3k；261887 29.2k vs
  8.7k），L2 接住 ~60%/24%，净 NVMe decode 加载 +30%/+143%；
  prefill L2 命中率仍≈0
- 决策：Phase D 默认 off（MoEResidencyEvictWeight 1→0；=1 重新启用
  实验，0=纯 tick-LRU bit-identical）；代码与开关保留。TTFT −4% 归因
  3b（Phase D 不改 prefill 加载量）。重建 e5b7c782 零警告 + 单测
  108/108。
- 在途：targeted-phased-d0.sh（3b only，同协议，15:34 启动，~17:00
  完成）取 3b-only 干净 TTFT/decode。
- 下一步：d0 定向 → 符合预期（TTFT ≈690/900 s、decode ≈9.99/9.23）
  则冻结 3b-only 候选并 commit → TTFT 主攻 prefill 侧（3a 分区 /
  prefill L2 驱逐）。GEMM 冻结不变。

## Phase D（项 4 驱逐权重优先）实现 + bitexact；定向干净重跑准备（2026-10-02 14:04）


Phase D（项 4）router 权重优先驱逐已实现（PlanResolve victim 引入 router
权重 EMA 时间衰减，最低期望未来需求优先、tie-break LRU；未观测专家 demand=0
回退纯 LRU；Q4T_MOE_EVICT_WEIGHT=0 回退纯 tick-LRU；ObserveRouterWeights
接线 moe.cu:446）。默认 on。bitexact-final 12288（base C=0 vs candfinal
C=256/Phase D on，1024/8192）BIT-EXACT=True 双档 → 不改输出（纯重排）。
文档缺口更正：Phase D 此前已实现但日志/STATUS 标"未开始"，现已补齐。
此前 targeted-c3b（13:13 二进制，仅 3b）因并发 q4t_tests 争 GPU/CPU 触发
deadline_exceeded（默认 20 min）失败作废；已 kill 冗余 q4t_tests（单测
108/108 已过）。下一步：targeted-phased.sh 干净重跑（Phase D on，
--request-deadline-ms 1800000）→ 对比 ed68cd3d 基线（Phase D off）的
重读因子/TTFT/decode → 依数据决定是否加 3a（分区）或调 Phase D decay。
GEMM 冻结不变。
## 新目标：四项内存中性 decode/TTFT 优化；Phase A/B 完成，Phase C 在途（2026-10-02 13:00）

用户设定新目标（2026-10-02）：在已验收最终候选（ed68cd3d，B=12288，
C=256 每层命中 top-n/L2-16/K=8/max-open-shards=200）基础上推进四项内存
中性优化：(1) 可观测性（per-request residency 行打印 mh/mw/msk + L2
decode/prefill 分列）；(2) 单 miss 快速路径（Q4T_MOE_INLINE_MISS_LIMIT）；
(3) prefill TTFT 三件套（sub-chunk 切分/相邻 pread 合并/投机补载，目标档
TTFT 937 s→≤656 s，重读因子 17.28→≤10）；(4) 驱逐策略权重优先（tick-LRU
基础上引入 router 权重/频率优先，45056 decode ≥+10%）。GEMM 冻结；改动
限定驻留层 + prefill 补载管线（moe.cu sub-chunk 切分/调度，不碰 GEMM
内核）；不得新增内存超支（峰值 ≤68.34 GB）；验收 = 六档 decode ≥60%
C=0 基线（当前 53–58%，50% 门槛不回退）+ 目标档 TTFT ≥30% 下降 +
bitexact + 质量/业务/生命周期。

Phase A（项 1）完成：per-request residency 行新增 mh/mw/msk + ld2*/lp2*
（L2 decode/prefill 分列），compare_e2e.py 解析扩展；纯打印不改计算；
bitexact B=12288（base C=0 vs candfinal）+ C=0 跨二进制（旧 ed68cd3d vs
新）双档全过。
Phase B（项 2）完成：Q4T_MOE_INLINE_MISS_LIMIT（默认 1，clamp [0,2]，
0=回退）——LoadPhase1 chunk 条目数 ≤ 阈值时调用线程内联 stage+commit，
跳过常驻 load worker 的 mutex+cv 派发/等待往返；多 miss chunk 保留原流水
线。bitexact 全过；45056 定向×3 decode 9.98/10.05/10.08 tps（vs 候选
9.9721 仅 +0.7%，目标 ≥10.97 未达）、TTFT 133.1–133.4 s 持平、内存峰值
无新增超支（fixed=63.76 GB 含镜像项）。前提"phase1 屏障/调度开销为主要
来源"被数据否定（dphase1 由实际 pread+swizzle+H2D 主导，worker 派发往返
仅微秒级）→ 内联路径保留为可配置项（默认 1），不靠它追 +10%；decode 主
杠杆为项 4（驱逐策略降 miss 数）与项 3（TTFT 三件套）。

下一步：Phase C（TTFT 三件套）冻结假设/改动范围/出口 → 实现 → bitexact
→ 目标档+204800 定向×3（TTFT/重读因子/加载量/decode）。GEMM 冻结不变。

## 最终候选验收完成：容量/性能/正确性 PASS，内存 USER_APPROVED_OVERRUN（2026-10-02 08:55）

final-acceptance B=12288（二进制 ed68cd3d，C=256 每层命中 top-n/
L2-16/K=8/max-open-shards=200；C3 协议基线 C=0 与候选同节奏）
06:44–08:55 完成，冻结口径 compare_e2e：

- 容量：目标档 in=261887+out=257=总上下文 262144，基线/候选各 3/3
  finish=length，服务端 token 计数核对，无截断 → PASS
- 性能（decode，hmean，每档 3 次，首/后续分列）：1024=53.8%、4096=
  53.9%、8192=53.0%、45056=55.5%、204800=58.1%、261887=54.2% →
  六档全部 ≥50% 基线 → PASS
- 正确性：六档 bit-exact yes；质量 11 题 manifest-exact + 基线/候选
  逐字一致；业务 6 项 bit-identical（8243/45107/204851）；生命周期
  （补载失败/取消/槽位复用/跨请求）verify-c1 六查 K=8 PASS → PASS
- 内存：候选 rss+gpu 峰值 61.056 GB + 模型页缓存增量 7.286 GB =
  68.34 GB vs 54 GB → USER_APPROVED_OVERRUN（超 14.34 GB，用户
  2026-10-01 07:25 授权 C=256 超支；记录实测峰值，非放宽 PASS，
  严格 54 GB 口径未达成）；基线 C=0 峰值 91.40 GB（参考）；swap
  used 峰值 1.49 GB（未掩盖预算）

验收报告定稿：docs/MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md §6b；
部署/回退：docs/MOE_RESIDENCY_DEPLOY_2026-09-30.md（K=0 回退边界
C1+C2+C3 已验证；--moe-resident-slots 0 回全常驻逐位不变）。
后续项：per-request residency 行补 mirror_hits 打印（可观测性，
不重跑矩阵）。GEMM 冻结（用户 10-02：目标完成前不改，之后另行
讨论）。


## C6 max-open-shards 修复：45056 decode 10.05 tps = 56% 基线，超 50% 门槛；final-acceptance 在途（2026-10-02 05:30）

C6（驻留层，GEMM 冻结）：c5b 分段计时定位 decode miss 临界路径主因不是
pread 而是 shard reopen——每个 decode miss 做 ~15 次 EnsureOpen，32 槽
open-shard LRU 在调用之间把 shard munmap 驱逐再重开（open+mmap+200 KB
header 解析，~1.5 ms），dpread 5.4 ms/miss（c5b：dstage 5.4/dpread
5.4/dphase1 4.6 ms，decode 6.45 tps=36%）。修复：Q4T_MOE_MAX_OPEN_SHARDS
（默认 32，0=索引内全部分片）；验收配置 200 ≥ 197 分片，全部保持打开
（数据仍走 pread、无进程页错误，mmap 仅触碰 header，不重复 09-19
OpenAllShards 回归——那次是启动预开全部分片且数据走 mmap，14 worker
页错误争 VMA 锁）。C6 试点（C3 协议，45056×3）：decode
9.97/10.05/10.12 tps（基线 17.84，=56%，门槛 50%=8.9），TTFT 133.5 s，
dpread 0.49 ms/miss、dstage 0.71、dphase1 0.88；post-warmup 页缓存
68.7 GB（C5 保留生效），MemAvailable 65.5 GB。

C5+C6 已并入 C3 验收协议（c3-pagecache-protocol.sh 把
Q4T_MOE_MAX_OPEN_SHARDS=200 传播到探针服务器与验收矩阵，protocol.log
记录有效值；基线 C=0 不受影响）。

下一步：final-quality-business B=12288（质量 11 题 + 业务 6 项 bitexact，
新二进制 ed68cd3d 受影响项重验）→ PASS 后 final-acceptance B=12288 全
矩阵（基线 C=0 + 候选，五档 + 261887 目标档，每档 3 次，内存门
USER_APPROVED_OVERRUN 口径）→ 报告定稿。GEMM 冻结（用户 10-02 再确认：
目标完成前不改，之后另行讨论）。

## C5 页缓存保留修复生效（decode 4.86→6.45 tps）；decode 分段计时在途（2026-10-02 02:55）

C5（驻留层，GEMM 冻结）两件事：(1) **页缓存保留**：open-shard LRU 驱逐
专家分片时，SafetensorsFile 析构原本对整片发 POSIX_FADV_DONTNEED，把
C3 预热填的页缓存页全抹掉，导致每次按需专家 pread 回退 NVMe（~4 ms）。
新增 SetKeepPageCache(true, "-experts-") 让专家分片跳过 DONTNEED（非专家
分片照旧）。pilot-45056（修前）vs pilot-45056-c5（修后）：pread_avg
4.0→0.87 ms，decode 4.86→6.45 tps（基线 17.84，=36%，门槛 50%），
TTFT 220→136 s；meminfo 页缓存 post-warmup 4.65→68 GB（保留生效）。
(2) **decode 分段计时**（C5 诊断）：dstage/dpread/dphase1 计数器已接线
并打印到 [residency][timing]，用于隔离 decode miss 临界路径（stage vs
pread vs phase1/H2D）。pilot-45056-c5b 在途（~12 min）取数。

单测：safetensors_keep_page_cache 新增；Tegra 6.8 内核 mincore 对常规
文件返回 0（功能不可用），测试改为先探测 mincore、不可用则仅验证 API
路径并跳过驻留断言（端到端效果由 pilot 覆盖）。108/108 通过。

差距：6.45 tps = 36% < 50% 门槛（8.9 tps）。定量：decode 25.8
miss/token × ~3.8 ms/miss ≈ 99 ms/token 开销（基线 56 ms/token）。
待 c5b 分段数据定位主因：若 dpread 高（页缓存被请求期内存压力逐出）→
保页/降压；若 dphase1 高（H2D/commit）→ 优化 H2D 路径；若 dstage 高
（锁/排队）→ 降竞争。GEMM 冻结不变（用户 10-02 再确认）。

下一步：c5b 分段 → 定位 decode miss 临界路径主因 → 在驻留层内修复
（GEMM 不动）→ 重验受影响项（bitexact/定向）→ final-acceptance
B=12288 全矩阵。

## [D] K=8 六查 PASS、path-c 已合并+重建；final-acceptance 前 45056 试点在途（2026-10-02 00:40）

[D] verify-c1 K=8（最终候选 C1+C2+C3+C4）23:46:46 六查全 PASS（FAIL=0，
预算 fixed=63.76 GB 含 1.06 GB 镜像项，K=8 口径正确）；queue-post-verify
全链 business_rc=0 / vc_K0_rc=0 / vc_K8_rc=0。path-c 已合并主工作分支
（82bae2c，00:08）并重建（BUILD_RC=0，00:30，build/q4t
d08a44fad57a）。

用户 10-02 三项指示：(1) 目标档口径=总上下文 262144（256k），不是输入
256k；不再纠结 261887/261888 一字之差——现目标档 in=261887+out=257=
262144（finish=length）即满足，验收矩阵不改。(2) 即使内存预算超支，
支持 C=256 每层按命中次数 top-n——即 B=12288（hot-final-12288.json，
与 hot-256.json 同集合，已核验），保持 USER_APPROVED_OVERRUN 口径
（内存门记录实测峰值，性能/正确性门不放宽）。(3) GEMM 冻结再确认：
目标完成前不修改，之后另行讨论。

final-acceptance 前风险复核：r3 c256 矩阵（B=12288 同席位配置，无
C1/C2/C3/C4）各档 decode 22–28% 基线；verify-c1 定向 45056（C1+C2+C4）
仅 +2–3% vs r3 c256（4.84–4.90 vs 4.74–4.76 tps）；设计文档自述 C1
对单 miss decode 层收益小（stage 仍在临界路径）。定量：50% 门槛需
≤2.2 ms/miss（现 25.6 miss/token×~5.8 ms≈148 ms 开销）。先跑 15 min
试点（pilot-45056.sh：C3 协议 drop_caches→启动→45056 预热×8，候选
配置 C=256/L2=16/K=8/Q4T_RESIDENCY_TIMING=1，45056×3 SSE 计时），
实测冷/热 pread_avg 与 decode tps，再决定是否直接进 ~5h final-
acceptance B=12288；若试点 <50%，先定位 decode miss 临界路径再跑
长验收。

下一步：试点结果 → （≥50%）final-acceptance B=12288 全矩阵；（<50%）
按 timing 分段定位并修 decode miss 临界路径（驻留层内，GEMM 不动）
→ 重验受影响项 → final-acceptance。GEMM 冻结不变。

## [C] verify-c1 K=0 六查全 PASS；[D] K=8 最终候选六查在途（2026-10-01 23:40）

[C] K=0（回退边界 C1+C2+C3）23:07:56 六查全 PASS（rc=0）：fault
（修后武装成功）、cancel、定向 45056×3 全快于 r3 c256（decode
+1.1/+1.5/+1.4%，TTFT -6.8~-8.4%）；预算 fixed=62.69 GB（无镜像
项）。[D] K=8（最终候选 C1+C2+C3+C4）23:08 启动在途（~80 min）。
更正：20:28 首跑标签"K=8"有误，预算证明实为 K=0（fixed 无 1.06
GB 镜像项）；其 fault FAIL（脚本缺陷）与定向 run1 -0.1%（噪声）
已被 [C] 复跑取代，首跑证据不用于验收。环境传播已用
/proc/environ 实测核验（[D] 服务器 K=8、L2=16）。业务 12/12
逐位一致 PASS（[A]/[B]）。合并 dry-run 已验证无冲突（代码取
path-c、文档取主分支）。GEMM 冻结（用户再确认）。

下一步：[D] K=8 六查 → 合并 C1+C2+C4 + 重建 → 冻结最终候选 B
（R3_DECISION 算法）→ final-acceptance → 报告定稿。

## 业务 12/12 逐位一致 PASS；verify-c1 K=0 回退边界六查在途（2026-10-01 23:30）

queue-post-verify 在途：[A] 业务 nu15552+c256 重跑（修参后，r3
二进制 efce31d8）6/6+6/6 OK；[B] 对 base 逐位比对 12/12
bitexact=True → BUSINESS-COMPARE PASS（post-r3-affected 业务证据
链闭合：质量 3/3 + 业务 12/12）。[C] verify-c1 K=0（回退边界
C1+C2+C3）六查在途（~80 min），随后 [D] K=8（最终候选）。
verify-c1 首跑（K=8）FAIL=1 原因：fault 脚本 env 行截断（已修）
+ 定向 45056 run1 decode -0.1%（噪声级；TTFT 三跑 -6.4~-7.9%，
设计 C1 出口为 TTFT 对比；双跑复测）。文档已按 C1+C2+C3+C4
口径更新（验收报告 §6b、部署回退 §3 C4 条目）。合并范围确认：
代码取 path-c（4 commit）、文档取主分支；主 build 重建等 [A]
结束（已完成）后、双跑结束后进行。GEMM 冻结（用户再确认）。

下一步：[C] K=0 → [D] K=8 → 合并 C1+C2+C4 + 重建 → 冻结最终
候选 B（R3_DECISION 算法）→ final-acceptance → 报告定稿。

## C4 已实现，verify-c1 六查 4 过 / 1 脚本缺陷已修 / 1 在途；验收队列在途（2026-10-01 21:00）

C4 驱逐镜像已实现于 path-c 分支（wt-c1 887a3fe，107/107 单测，
含 residency_mirror_ring_hit）：每层 K=8 pinned 环（~1.06 GB，
Q4T_MOE_MIRROR_K=0 关闭=回退边界 C1+C2+C3），CommitExpert
stream-ordered D2H 写回，StageExpert miss 先查环再选 L2 victim。
verify-c1（K=8）在途：单测/bitexact/跨二进制恒等/cancel 四项
PASS；fault FAIL 为脚本 env 行截断（钩子未武装，已修）；定向
45056×3 在途。post-r3-affected：质量 3/3 PASS、业务 base 6/6
OK；业务 nu15552+c256 因旧版脚本缺 `--` 失败（已修，待重跑）。
queue-post-verify（setsid）在途：当前 verify-c1 结束后自动
业务重跑+逐位比对 → verify-c1 K=0 → verify-c1 K=8（PATH_C
验收顺序双跑）。GEMM 冻结（用户再确认：目标完成前不改，之后
另行讨论）。

下一步：跟踪队列 → C1+C2+C4 合并主分支 → 冻结最终候选（B 按
R3_DECISION 算法）→ final-acceptance → 报告定稿。

## r3 矩阵完成：两候选均 <50%（c256 22–28% / nu15552 28–37%），§5 分支 3 确认（2026-10-01 17:10）

nu15552 六档 3/3 全完成（17:06:33，rc=0，18/18）：
- c256（C=256 均匀+hot-256+L2-8）：decode 24.8/23.3/22.0/26.6/
  28.1/23.1% 基线，逐位一致 6/6，峰值 58.86 GB（授权超支）。
- nu15552（按层 top-n，C_l 256..446，总 15552，L2-8）：decode
  37.0/30.2/28.4/32.5/36.9/29.4% 基线，逐位一致 6/6，峰值
  68.07 GB（授权超支）；261887 目标档合同 3/3（in=261887
  out=257 finish=length，总 262144）。席位扩大使 261887 加载量
  2.89M→1.56M（−46%）、NVMe 7.99→4.31 GB/请求，decode 仅温和
  增益（+6–9pp）→ 瓶颈为稳态 miss 临界路径成本（6.2 ms/miss），
  非席位数量。
- §5 分支判定：**分支 3**（两候选均 <50%）→ C4（驱逐镜像）+ C3
  （页缓存预热）组合复测；主杠杆 C1+C2（miss 6.2→2 ms 时 C=256
  decode ≈54%）。auto-c1-verify 将自动写 section5-branch.txt 并
  跑 verify-c1 六查（C1+C2 二进制，含 45056 定向对比 r3-c256）。
- post-r3-affected 在途（17:06:33 启动：质量 11 题×3 组 + 业务
  6 请求×3 组，r3 二进制）。

下一步：跟踪 post-r3-affected → auto-c1-verify（分支 3 + verify-c1）
→ 合并 C1+C2 → 实施 C4（分支 3 触发）→ 冻结最终候选（C1+C2+C3+C4，
B 按 R3_DECISION 算法）→ final-acceptance → 报告定稿。GEMM 冻结
（约定持续）。

## nu15552 矩阵 204800 档 2/3 完成、请求 3 在途；final-acceptance B=12288 契约复核通过（2026-10-01 16:10）

矩阵在途（不触碰 GPU/页缓存）：
(1) 进度：nu15552 204800 档 2/3 完成（auto-12/13：in=204800 out=256
finish=length，loads≈1.207M、nvme≈3.33 GB/请求），请求 3 在途
（15:24 启动，~14.7 min/请求，ETA ~16:10）；261887 目标档（总上下文
262144）~16:10 启动，预计 ~17:30 完成。c256 六档已定（decode
22–28% 基线、逐位一致 6/6、rss+gpu 峰值 58.86 GB）。
(2) final-acceptance B=12288（C=256 每层 top-n，用户授权超支）契约
复核：final-acceptance.sh B 白名单含 12288，USER_APPROVED_OVERRUN
标志在位（内存门按用户授权口径记录实测峰值，性能/正确性门不放宽）；
hot-final-12288.json/cap-final-12288.json 在位（48×256=12288，15:30
已核验）。
(3) 自动链健康：auto-r3c (2621362) → queue-r3-final (2622739，
nu15552 在途) → auto-c1-verify (2639799，轮询 auto-r3c 退出后做
§5 分支判定 + verify-c1 六查)。GEMM 冻结（约定持续：目标完成前不
修改，之后另行讨论）。

下一步：跟踪矩阵 → compare 报告 → §5 分支 → post-r3-affected →
verify-c1 → 合并 C1+C2 → 冻结 B → final-acceptance → 报告定稿。
## 内存门页缓存口径改矩阵末态（含长档额外专家）；C3 协议补 meminfo-06（2026-10-01 16:05）

nu15552 矩阵在途（204800 请求 3，随后 261887 目标档 ~18:00）期间：
final-acceptance [4/4] 内存门原用预热页缓存增量（45056 预热），但验收
矩阵含 204800/261887 长档会加载额外专家 → 矩阵末态页缓存更大，54 GB
硬门用预热增量会低估。已修：C3 协议 --acceptance 矩阵后补
meminfo-06-post-matrix，evidence.json 增 page_cache_delta_matrix_kb
（矩阵末态稳态页缓存）；final-acceptance pcache() 优先矩阵末态、缺失
回退预热（兼容旧证据）并打印来源。模型加载瞬态页缓存不计稳态预算。
GEMM 冻结（第十一次确认）。

下一步：跟踪矩阵 → compare → §5 分支 → post-r3-affected → verify-c1 →
合并 C1+C2 → 冻结最终候选（含 C3）→ final-acceptance → 报告定稿。

## C3 协议补全 warm pread_avg 证据采集；自动链契约复核通过（2026-10-01 15:55）

nu15552 矩阵在途（204800 档 2/3，随后 261887 目标档 ~18:00）期间：
(1) C3 协议 --acceptance 分支补 Q4T_RESIDENCY_TIMING=1（基线/候选同设，
C=0 零开销）+ 矩阵后从 e2e server.log 提取 warm pread_avg 入
evidence.json，完成设计 §7.3 冷→暖证据对（原脚本仅采集冷侧，r3 矩阵
server.log 实测 0 timing 行）；(2) auto-r3c/auto-c1-verify/verify-c1/
queue-r3-final 全链 tag/正则/文件契约与 compare_e2e.py 实际输出逐行
核对一致。GEMM 冻结（第十一次确认）。

下一步：跟踪矩阵 → compare 报告 → §5 分支 → post-r3-affected →
verify-c1 → 合并 C1+C2 → 冻结最终候选（含 C3）→ final-acceptance →
报告定稿。

## nu15552 矩阵 4/6 档完成；部分分支读数：两候选均 <50%，§5 分支 3 大概率（2026-10-01 15:25）

nu15552（按层 top-n、DP 最优 C_l 256..446、总 15552 槽、≈68.8 GB
授权超支）15:24 完成 45056 档（3/3，逐位一致）；四档 decode
35–39%/30%/28%/32% 基线，全部 <50%。204800 在途（15:24:15 启动），
随后 261887 目标档（总上下文 262144），预计 ~20:00 完成。

部分分支读数：c256 六档 22–28%（已定）+ nu15552 四档 28–39% → §5
分支 3（C4 驱逐镜像 + C3 页缓存重测，或按失败维度报告差距）大概率。
席位扩大仅温和增益，确认瓶颈为稳态 miss 临界路径成本（6.2 ms/miss
有效），主杠杆为 C1+C2+C3（冻结依据：miss 6.2→2 ms 时 C=256
decode ≈54% 基线）。自动链 auto-r3c + auto-c1-verify 在途健康；
等待期间不触碰 GPU/页缓存。GEMM 冻结（第十一次确认）。

下一步：跟踪矩阵 → compare 报告 → §5 分支 → post-r3-affected →
verify-c1 → 合并 C1+C2 → 冻结最终候选（含 C3）→ final-acceptance →
报告定稿。

## final-acceptance.sh 契约修复（B=10752 白名单 + 页缓存精确字节）；r3 矩阵 c256 目标档 2/3（2026-10-01 14:45）

矩阵在途（c256 261887 目标档 2/3，ETA ~15:02；261887 档首请求已按合同
完成：in=261887 out=257 finish=length，总上下文 262144）完成 CPU 侧
契约修复：(1) final-acceptance.sh B 白名单补 10752（§5 分支 1/2 且
C≥224 时的条件候选，名单 14:32 已核验在位）；(2) 内存门页缓存增量改
精确字节（原 GiB×1e9 低约 6.9%，54 GB 门边界可能误判）。部署文档阶段
快照已提交推送（21d48eb）。c256 rss+gpu 峰值目前 58.84 GB（swap
1.10 GB，分列不掩盖），超预算为已授权测量。GEMM 冻结（第十一次确认）。

下一步：跟踪矩阵 → nu15552（cap=446 按层 top-n）→ post-r3-affected →
auto-c1-verify（§5 分支 + verify-c1 六查）→ 冻结 B → final-acceptance
→ 验收报告定稿。

## 最终候选 B 选择算法入决策包 + C3 协议 --acceptance L2 传播修复；r3 矩阵 261887 目标档在途（2026-10-01 14:20）

矩阵在途（c256 261887 目标档请求 2/3，ETA ~15:03；c256+L2-8 实测
rss+gpu 峰值目前 58.83 GB，超 54 GB 预算、用户已授权测量）完成：
(1) 最终候选 B 选择算法入 R3_DECISION 末节：B ∈ {7680,8448,9216,9984}
（§5 分支 1/2 且 C≥224 时追加 10752），按层 DP 最优 C_l（cap-final-<B>），
同一候选须同时满足内存（rss+gpu 峰值+模型页缓存增量 ≤54 GB，swap
分列不掩盖）、性能（六档 decode 调和均值 ≥50% 基线，每档 3 次、
首/后续分列）、正确性（bit-exact+质量+业务+受影响检查）三条，取
满足者中最大 B；无一满足则按失败维度报告差距，不放宽目标。
(2) 验收链契约核验：final-acceptance [4/4] 内存门口径与 ledger 一致；
261887 档基线/候选工作负载身份一致（输入 261887+输出 257、3 次、
无 warmup）；质量 11 题/业务 6 请求 fixture 与 wt-c1 二进制在位。
(3) C3 协议 --acceptance 分支 L2 未传播真缺陷修复（内存门 17.8 GB
虚高风险；C=0 基线不受影响；bash -n 通过）。

下一步：跟踪矩阵 → §5 分支 → 冻结 B → final-acceptance → 验收报告
定稿。GEMM 冻结（第十一次确认）；261887/261888 一字之差不再纠结，
目标档口径=总上下文 262144。

## C3 页缓存协议脚本就绪；C4 locality 证据入设计文档；r3 矩阵 c256 五档完成、261887 在途（2026-10-01 13:55）

矩阵在途（不触碰 GPU/页缓存）完成两项准备：C3 协议脚本
c3-pagecache-protocol.sh 落地（设计文档 §7：guard → drop_caches
冷基线 → 计时启动 → 45056×1 受控预热丢弃 → 页缓存增量/pread_avg/
重读因子证据 → 可选 --acceptance 同配置直连全矩阵；参数化
tag/C/hot-list/L2/binary，C=0 走全专家常驻基线路径）；guard 实测
矩阵在途正确拒绝。C4 locality 证据补入设计文档（主研究 20 请求：
再请求局部性仅 lag≤16 显著，lag32 0.94×/lag64 0.66× vs 随机 →
C4 收益温和 decode +3–5%，支持 C1/C2+C3 先行、C4 仅 §5 分支 3
时实施）。

r3 矩阵（13:52）：c256 五档 3/3 全过（decode 4.54/4.12/3.95/4.74/
4.14–5.21 tps，204800 TTFT 1166–1168 s），261887 目标档在途
（prefill layer 39/48）；随后 nu15552（cap=446 按层 top-n，用户
授权超支）→ post-r3-affected → auto-c1-verify/verify-c1。
GEMM 冻结（第十一次确认：目标完成前不修改，之后另行讨论）。

下一步：跟踪矩阵 → §5 分支决策 → C3 协议（最终候选 C 冻结后）→
最终验收。

## auto-c1-verify GATE 解析 bug 修复 + compare 加固 + 旧报告隔离；C1+C2 审查通过（2026-10-01 13:45）

矩阵在途（c256 204800 档 3/3）期间修复自动链隐患：auto-c1-verify 的
GATE 解析对 `ALL PASS` 只捕获 `ALL`（判定 `== 'PASS'` 永不成立，r2
全 FAIL 未暴露）→ 已归一化三态；compare_e2e.py 加固（zero-tps
ZeroDivisionError 根因、缺失文件→PENDING、均值用 len）并自测通过；
10:03 旧 traceback 报告隔离为 *-STALE-20261001T1003（新报告矩阵后
重新生成，缺失则安全停止）。C1+C2（df02f60）静态审查：C2 几何、
槽位唯一性、stream 顺序、错误路径、统计、内存账 4.68 GB 全部一致，
verify-c1 六查与 fixture 依赖核验，未发现正确性问题。GEMM 冻结
（第十一次确认）。

## GEMM 冻结（第十一次确认）：目标完成前不修改，之后另行讨论（2026-10-01 13:32）

用户：GEMM 暂时不修改，等当前目标完成后再讨论。记为第十一次确认，并新增
“目标完成后另行讨论”约定；期间任何改动均不触碰 GEMM 文件。git 复核：
GEMM 文件最后改动 2026-09-27（2782ef0），C1+C2（df02f60）至 HEAD 的 diff
无 GEMM 文件。r3 矩阵在途（c256 204800 档 1/3），不受影响。

## 隔离 10:04 无效 post-r3-affected（旧 auto-r3b 触发）；r3 矩阵在途（c256 204800 档 1/3）（2026-10-01 13:25）

旧 auto-r3b 观察器在第一次矩阵尝试失败后于 10:04 触发 post-r3-affected
（二进制 7f013d73，L2 修复前）：quality baseline 10/11 成功，第 11 条
（204800 token 提示）客户端挂起 ~353 s 且从未到达服务端（server.log 无
auto-10，shutdown 0 in-flight；服务端 30 s 读截止下到达必有痕迹）。同一
fixture 08:43 曾 11/11 成功；单次发生按瞬态处理。部分输出已隔离为
affected/r3-quality-base-INVALID-20261001T1004（必需：run_acceptance.py
拒绝非空输出目录，不隔离则真实 post-r3-affected（auto-r3c 3/3，~18:30）
会在 [1/4] 失败）。若再现，用 Q4T_ACCESS_LOG=1 harness 外复跑取 HTTP 痕迹。
矩阵（13:22）：c256 四档完成（decode ~24–28% 基线，与 r2 一致、优化前
预期内），204800 在途 1/3，随后 261887 → nu15552 → post-r3-affected →
auto-c1-verify（§5 分支 + verify-c1）。GEMM 冻结（第十次确认）。

## C1+C2 实现完成并提交（path-c 分支 df02f60，UNVERIFIED 阶段快照）；r3 矩阵在途（c256 五档中）（2026-10-01 13:05）

C1（stage→commit 流水线，worker 8→16）+C2（scale 4→1、gate/up
2→1，每专家 9 次 H2D→5 次）按冻结设计实现，提交
codex/moe-residency-path-c-20261001（df02f60）。并发安全审查通过
（plan 内专家/槽位唯一、commit 全部入单模型流、失败路径
ReleaseMissClaim、计数器 worker 局部合并）；wt-c1 独立构建零警告
（q4t ba7327a9），主树工作区已还原。GPU 验证按计划在 r3 矩阵报告
后进行（C1 验证配置 C=256+hot-256+L2-16，[q4t][budget] 逐项核对）。
r3 矩阵（13:04）：c256 四档过、204800 prefill 在途，随后 261887 →
nu15552（cap=446 按层 top-n，授权超支）→ post-r3-affected。
GEMM 冻结（第十次确认）。

## r3 逐 miss 计时完成；路径 C 设计冻结（C1 流水线/C2 H2D 合并/C3 页缓存/C4 驱逐镜像/C5 席位）（2026-10-01 12:35）

timing-collect-r3（efce31d8，C=256+hot-256+L2-8，45056+256）：
NVMe pread 2.40 ms/专家（页缓存 3.7 GB 冷读主导）、phase1
3.59 ms/chunk、d2h 3.12 ms/层、L2-8 命中 0、重读因子 17.5×
（1.19 TB/请求）、decode miss 25.7/token、TTFT 288 s（基线
30.55 s）。临界路径模型：decode ≈160–170 ms/token（实测
206–221），瓶颈 = NVMe stage 在 CPU 临界路径。否定结论：预取
上一步路由对 decode 无效（miss 专家必不在上一步 needed 集）。
路径 C 设计冻结（MOE_RESIDENCY_PATH_C_DESIGN_2026-10-01.md）：
C1 流水线 stage→commit+worker 8→16（先做）、C2 H2D 合并、
C3 页缓存策略（54 GB 含页缓存口径 → 最终候选 C≈160–224，
miss 单价降至 ~0.25 ms，decode 可至 ~95% 基线）、C4 驱逐镜像
（视 r3 结果）、C5 席位（r3 矩阵测量中）。决策逻辑三分支见
§5。GEMM 冻结（第九次确认）。

r3 矩阵 12:14 启动（auto-r3c，r3-cand-c256 → r3-cand-nu15552，
六档×3，基线复用 r2-baseline-s0），预计 ~22:00–23:00 出报告。

下一步：跟踪矩阵 → 报告后按 §5 决策 → 实施 C1+C2 → 定向验证。

## 第三轮矩阵首跑失败（L2 miss 竞态）→ 修复验证全过（efce31d8）→ auto-r3c 重跑链已启动（2026-10-01 12:10）

09:59 启动的第三轮矩阵两候选均在 ~1.5 min 内 rc=1（1024 档
out=28/46 提前 EOS）。根因：L2 miss 路径竞态——StageExpert miss
分支分配 L2 缓冲时未置 l2_claimed_，同 chunk 并发 miss 可经
PickL2Victim 选中该缓冲作 victim，NVMe 读未完成时覆写 payload →
输出损坏（L2-8/9 下 3/3 DIFF，L2-128 下 3/3 MATCH，与 r2 矩阵
bit-exact 6/6 一致）。修复（仅驻留层，GEMM 冻结第八次确认）：miss
路径同锁内分配后立即 claim，CommitExpert 成功释放，全部错误路径
经 release_miss_claim 释放。验证（efce31d8）：q4t_tests 106/106；
L2-8 5/5、L2-128 2/2 与 r2-baseline-s0 content 逐字一致
（compare-l2fix.py OVERALL: PASS）；bitexact-nu（cap=446+
hot-nu-15552+L2-8）1024/8192 BIT-EXACT。基线复用 r2-baseline-s0
仍有效（C=0 路径未受修复影响，跨二进制一致性经 L2-8 A/B 5/5
再确认）。

12:09 auto-r3c（pid 2621362，setsid）重跑链启动：
timing-collect-r3（efce31d8，L2-8，单条 45056，逐 miss 五段
计时——此前 timing-collect 跑在无 timing 特性的 r2 二进制上，
数据缺失）→ queue-r3-final（r3-cand-c256 → r3-cand-nu15552，
六档×3，基线复用 r2-baseline-s0）→ post-r3-affected（质量×3+
业务×3），日志 auto-r3c.log。预计 ~22:00–23:00 出两份 compare
报告。GEMM 冻结（第八次确认；git 核验 GEMM 文件最后改动
2026-09-27，本轮 diff 仅驻留层）。

下一步：跟踪 auto-r3c → 依据 r3 矩阵与逐 miss 计时数据定路径 C
（补载管线优化：r2 证据 25.7 miss/token × ~5.8 ms/miss ≈
150 ms/token 开销，需降至 ~2 ms/miss 量级才可能过 50% 门槛）。

## 五查门全过（7f013d73）；第三轮矩阵运行中（r3-cand-c256 → r3-cand-nu15552，六档×3，~20:00 出报告）（2026-10-01 09:59）

五查门全过（09:33–09:59）：q4t_tests 106/106；bitexact-c256
BIT-EXACT×2；bitexact-nu BIT-EXACT×2（按层非均匀 C 路径数值与
C=0 逐位一致）；跨二进制 C=0 逐位一致×2（基线复用 r2-baseline-s0
有效）；fault（500+注入消息 → 200+逐位一致）；cancel（取消后重发
与基线一致）。09:59:27 自动启动 queue-r3-final.sh（pid 2595010）：
r3-cand-c256（C=256+hot-256+L2-8）→ r3-cand-nu15552（cap=446+
hot-nu-15552+L2-8，用户授权超支），六档×3，预计 ~20:00 完成出两份
compare 报告；auto-r3b 随后自动 post-r3-affected（质量×2+业务×2）。
GEMM 冻结（第七次确认）。
下一步：跟踪矩阵 → 回填验收报告 §6 / 内存账（budget_backfill.py）/
R3 决策包定稿。

## 第三轮二进制构建修复（TimingStats 原子量破坏可移动性）；auto-r3-resume 链重启（2026-10-01 09:35）

auto-r3 链 09:20 在重建步骤失败停止（设计行为）：62edd6a 新增的
MoEResidency::TimingStats 含 std::atomic（非可移动），使
DecoderLayer 隐式移动构造被删除，model.cu `layers.resize` 编译失败
（此前 g++ -fsyntax-only 单文件语法检查未覆盖 libstdc++ 模板实例化）。
修复：TimingStats 装箱 unique_ptr（与 l2_mu_ 堆分配保可移动性模式
一致），21 处记录点改 `timing_->`，model.cu 保持原 resize；完整构建
零警告，二进制 7f013d73。GEMM 冻结（第七次确认，本轮仅驻留层
头文件/实现）。

执行链：auto-r3-resume.sh（09:33，setsid）重建（no-op）→ 五查门
verify-r3-binary.sh（在途 ~40 min：q4t_tests 106 + bitexact-c256 +
bitexact-nu + 跨二进制 C=0 逐位一致 + fault + cancel）→ 全过自动
启动 queue-r3-final.sh（r3-cand-c256 C=256+hot-256+L2-8 →
r3-cand-nu15552 cap=446+hot-nu-15552+L2-8，六档×3，基线复用
r2-baseline-s0）→ 两份 compare 报告；auto-r3b 观察器存活，矩阵后
自动 post-r3-affected（11 题质量×2 + 业务×2）。任一门失败即停。
timing-collect 已完成（第二轮二进制 45056 单条：loads=430524、
load_mb≈1.19 TB、misses=418236、L2 命中 0.16%）。预计 ~20:00
前后出 r3 两份对比。

## 第二轮矩阵完成：六档 decode 21.6–29.6% 基线（全 FAIL，如实记录）；post-r2-affected 完成（2026-10-01 09:12）

第二轮全矩阵（f2e9de7f，05:29–08:42）完成：基线 C=0 与候选
C=256+hot-256+L2-128 均六档×3 全过，逐位一致 6/6，目标档 token
合同 3/3（in=261887 out=257 finish=length，总上下文 262144）。
decode 比值 1024/4096/8192/45056/204800/261887 = 0.250/0.229/
0.216/0.254/0.296/0.229，全部 <50% 门槛（FAIL 如实记录，compare-
report-r2.txt）。L2-128 实测命中率 0.5–4.2%（261887 档
l2h=3040/l2m=2.89M≈0.1%），基本无效，与离线预测一致。内存峰值
（memory-peak.json 权威口径 rss+gpu）：基线 91.35 GB / 候选
77.74 GB（gpu 90.40/56.24 GB）；候选 TTFT 261887 档 1472.7 s
（基线 213.1 s，prefill 流式主导）。

结论不变：达标需同时 (a) 增 GPU 席位（nu-15552，已授权）与
(b) 控制补载开销（L2-128 弃用，第三轮降 L2-8 隔离 C 效应；
逐 miss 五段计时 timing-collect 将在第三轮重建前采集）。

执行链状态：post-r2-affected 在途（08:43 启动，11 题质量集
基线/候选 + 6 请求业务对照，预计 ~09:30）→ auto-r3 自动链
（timing-collect → 重建第三轮二进制 → 五查门 → r3 矩阵
r3-cand-c256 + r3-cand-nu15552，六档×3，基线复用 r2-baseline-s0）
→ auto-r3b 观察器（矩阵完成后自动跑 post-r3-affected：r3 二进制
上 11 题质量×2 配置 + 业务对照×2，最终候选的直接证据）。
GEMM 冻结（第六次确认，本轮零触碰）。

## 第三轮已获用户授权（按层 top-n nu-15552 + C=256 对照，超支接受）；auto-r3 将自动执行计时采集→重建→五查门→第三轮矩阵（2026-10-01 07:30）

用户决定（07:25，已记入
[MOE_RESIDENCY_R3_DECISION_2026-10-01.md](MOE_RESIDENCY_R3_DECISION_2026-10-01.md)
"用户决策"节）：(1) "即使内存预算超支，我支持你把C=256也做了，每层专家
按照命中次数的top-n来" → 第三轮执行 **nu-15552**（按层非均匀、每层专家
按冻结 business-set-v1 校准切分命中次数 top-n、DP 最优 C_l 256..446、
总 15552 槽、≈68.8 GB）+ **C=256 均匀同轮对照**；内存超支（超已接受
C=256 的 58.49 GB 约 9–10 GB）已明确接受。(2) "GEMM 暂时不修改,我们等
当前目标完成后再讨论" → GEMM 冻结（第六次确认；git 核验 GEMM 文件最后
改动 2026-09-27，本轮零触碰）。

执行链（auto-r3.sh 07:25 更新版，setsid 常驻 pid 2557253）：第二轮矩阵
结束 → timing-collect（第二轮二进制 f2e9de7f，C=256+hot-256+L2-128，
单条 45056，逐 miss 五段计时，路径 C 诊断数据）→ 重建第三轮二进制 →
五查门（q4t_tests 106 + bitexact-c256 均匀回归 + bitexact-nu 非均匀 +
跨二进制 C=0 逐位一致 + fault + cancel，全部通过才继续）→
queue-r3-final.sh：r3-cand-c256（C=256+hot-256+L2-8）→
r3-cand-nu15552（cap=446+L2-8），六档×3，基线复用 r2-baseline-s0（门
3b/5 验证跨二进制 C=0 逐位一致后有效）→ 两份 compare 报告。任一门失败
即停，不启动矩阵、不自动重试。

第二轮矩阵在途（f2e9de7f，07:15 时点）：基线 C=0 六档×3 全过（decode
16.8–18.7 tps，261887 目标档 ttft≈213 s/decode≈16.8 tps/逐位一致/总
上下文 262144）；候选 C=256+hot-256+L2-128 已过 1024/4096/8192/45056，
204800 在途（06:23 起，每请求 ~20 min），261887 待跑，预计 09:00–09:30
全矩阵完成，随后 post-r2-affected（11 题质量集+6 请求业务对照）。在途
证据不变：L2-128 命中率极低（4096~0.7%、45056~0.17%）基本无效且新增
~19 GB 超支；第一轮 decode 22.0–29.8% 基线全 FAIL，第三轮 nu-15552
离线 sim miss 10701（较均匀 C=324 少 16.5%）为同内存最优。

## 第二轮候选在途（204800 1/3）；补载管线分析+逐 miss 计时采集就绪，A/C 决策待授权（2026-10-01 07:00）

第二轮矩阵在途（f2e9de7f）：基线 C=0 六档全过（decode 16.8–18.7 tps，
261887 目标档 ttft≈213 s/decode≈16.8 tps/逐位一致/总上下文 262144）；
候选 C=256+hot-256+L2-128 已过 1024/4096/8192/45056，204800 在途
（1/3，每请求 ~20 min），261887 待跑，预计 09:00–09:30 全矩阵完成。
实测 decode 比值 1024=24.7%/4096=22.8%/8192=21.5%/45056=25.9%（全
<50% FAIL）。关键证据：L2-128 命中率极低（4096~0.7%、45056~0.17%、
204800 l2h≈2339/l2m≈2.23M~0.1%）基本无效，且新增 ~19 GB 超支（候选
实测峰值 rss+gpu≈77.7 GB vs 已接受 C=256 58.49 GB；swap 1.03 GB 单列）。

补载管线（路径 C）静态分析完成（纯代码，GEMM 冻结未动）：decode 热路径
每层 D2H+`cudaStreamSynchronize`（moe.cu:418–434，基线/候选共有，非
减速之源）；单 miss 有效 6.2 ms = NVMe ~1.44–2 ms + ~4 ms 管线开销
（stage/commit 串行无重叠 + 逐专家串行 H2D + 互斥/条件变量往返，见
06:38/07:00 日志）。逐 miss 五段计时（Q4T_RESIDENCY_TIMING=1）代码已
就位但此前未开启；timing-collect.sh 已备好（候选同配置单条 45056，
~5 min），待第二轮释放 GPU 后立即运行（两服务并存 ~155 GB > 122 GB
RAM，不可并行）。R3 决策包补「补载管线静态分析」节。

**待用户授权**：路径 C（推荐，弃 L2-128、保持 C=256=58.49 GB、优化补载
管线，定量 decode≈54% 基线过门槛）/ 路径 A（增 C：nu-15552 推荐
≈68.8 GB +10.3 GB / C=324 / C=384）/ A+C 并行。第二轮实测落地后回填
决策包定稿。GEMM 冻结（第五次确认）。

## 第二轮基线完成、候选在途；第三轮决策包成文，GEMM 冻结第五次确认（2026-10-01 06:20）

第二轮矩阵在途（f2e9de7f，05:29 启动）：基线 C=0 六档×3 全过（05:29–05:55，
26 min；261887 档 ttft≈213 s、decode≈16.9 tps、逐位一致、总上下文 262144）；
候选 C=256+hot-256+L2-128 已过 1024/4096/8192、45056 档在途，预计 12:00 前后
全矩阵完成。在途关键证据：**L2-128 命中率极低**（4096 档 ~0.7%、45056 档
~0.17%），对该工作集基本无效，与离线预测一致；decode 瓶颈定量（45056 档反推）：
基线 decode≈15.1 s，候选 dmiss≈6500×~6.2 ms 串行≈40 s → decode_tps≈28% 基线，
与第一轮 22–30% 吻合；**达标需把 decode miss 降 ~2.7×**。

第三轮决策包成文：
[MOE_RESIDENCY_R3_DECISION_2026-10-01.md](MOE_RESIDENCY_R3_DECISION_2026-10-01.md)。
推荐 **nu-15552**（按层非均匀、每层专家按校准集命中次数 top-n、DP 最优
C_l 256..446、总 15552 槽、≈68.8 GB、同内存比 C=324 均匀少 16.5% miss），与
用户"每层专家按命中次数 top-n"口径一致；备选 C=324 均匀（≈68.8 GB）/
C=384（≈76.7 GB）；不新增内存备选：decode 导向热点表（C=256 保持 58.49 GB）
或补载管线并行优化（GEMM 仍冻结）。第三轮属新增内存超支（+10.3 GB 相对已接受
C=256），auto-r3.sh 只到五查门+决策摘要、不自动启动矩阵，**待用户明确授权**。
GEMM 冻结第五次确认；工作树 diff 仅驻留层/服务层/测试工具与文档，无 GEMM 改动。
261887/261888 边界不再追究（目标档=总上下文 262144=输入 261887+输出 257）。
下一步：跟踪第二轮矩阵 → compare-report-r2.txt 实测列回填决策包 → 向用户提交
第三轮授权决策。

## 第二轮矩阵运行中；第三轮（按层非均匀 C）准备完毕，待超支决策（2026-10-01 05:55）

门 #4（f2e9de7f）05:29 四查全过（q4t_tests 106/106、bitexact-c256
BIT-EXACT×2、affected-fault、affected-cancel）；auto-r2c 05:29 自动启动
第二轮矩阵（L2-128+C=256+常驻池，六档×3，r2-* 标签）：基线 C=0 已过
五档、正在 261887 档，随后候选档。第一轮实测 decode 22.0–29.8% 基线
（全 <50% 门槛 FAIL，根因逐 miss 逐层串行 NVMe 补载）；第二轮 L2-128
离线预测 ~30%，预计仍低于门槛，达标需第三轮。

第三轮准备完毕（代码未验证；build/q4t 在第二轮全程保持 f2e9de7f，
矩阵不受影响）：(1) model.cu 按层非均匀 C——c_layer[l]=min(热点表长,
--moe-resident-slots 上限)，均匀表保持旧行为；运行时路径（MoEResidency
每层 Init/Slots/Layout、moe.cu prefill 分组与 C<topk 守卫、decode 核
per-expert 步长）结构上按层安全，已逐项复核；(2) chat_server.cpp 内存
预算估算改按层实际驻留数（旧式按均匀 C，nu-15552 cap=446 时会把权重
少计 ~16.2 GB），槽字节改用 MoEResidencyLayerBytes 精确值；(3)
tools/trace/make_hot_list_nu.py + hot-nu-15552.json（DP 最优按层 C_l
256..446、总 15552 槽，sim miss 10701 vs 均匀 C=324 的 12818，-16.5%）
+ hot-324/384.json + queue-r3.sh/queue-r3-nu.sh + bitexact-nu.sh +
verify-r3-binary.sh（五查门：q4t_tests + bitexact-c256 均匀回归 +
bitexact-nu 非均匀 + fault + cancel）。auto-r3.sh（setsid）等 auto-r2c
结束 → 重建 → 五查门 → 决策摘要；不自动启动第三轮矩阵。内存估算
（槽 2,764,816 B，第一轮 C=256 实测 58.49 GB 为基准）：C=324/nu-15552
+L2-8 ≈ 68.8 GB、C=384+L2-8 ≈ 76.7 GB——均超出用户已接受的 C=256
超支（58.49 GB），启动第三轮需用户明确授权。GEMM 冻结（第四次确认）。

## 四查门 #4 运行中（新二进制 f2e9de7f）；fault 钩子全局一次性修复（2026-10-01 05:10）

四查门 #3（04:49，二进制 b22a1049）：q4t_tests 106/106 PASS、
bitexact 两档 BIT-EXACT=True PASS、affected-cancel PASS（取消后重发
逐位一致）；affected-fault FAIL 且暴露两个缺陷：(1) "first" 钩子按层
布防——每层实例各自 arm，step1 在 layer 0 首个 stage 失败后，step2
又在 layer 1 首个 stage 失败（server.log diag 证实：step1 死于
layer 0、step2 死于 layer 1），恢复合同（step2 必须成功）被破坏；
(2) 错误消息在 LoadPhase1 被压成 "residency parallel stage failed"、
在 ChunkPrefillReq（仅 bool ok）再次丢失，HTTP 500 正文不含
"fault injection"，测试无法验证失败原因。

修复（仅驻留层+服务层，GEMM 冻结未动；二进制 f2e9de7f，05:05
零警告构建）：(1) "first" 钩子改全局一次性（g_first_fault_fired
原子量，全模型恰好一个请求期 stage 失败；expert-id 模式语义不变）；
(2) 错误消息全链传播：LoadPlan.first_err_msg（首个失败 worker 写入，
cv 等待建立 happens-before）→ LoadPhase1 "residency parallel stage
failed: <msg>" → ChunkPrefillReq.err → HTTP 500 正文
"scheduled chunk prefill failed: residency fault injection: expert N
(test hook)"。门 #3 日志封存 verify-r2-binary.log-run3-b22a1049。
05:07 启动门 #4（f2e9de7f，setsid）；auto-r2c.sh 观察门 #4，四查全过
才 setsid 启动第二轮矩阵（queue-r2-l2pool.sh，L2-128+C=256+常驻池，
六档×3，r2-* tag）→ 再跑 post-r2-affected.sh（11 题质量集+6 请求
业务对照）。NVMe 单 miss 分解（05:05 实测）：3.28 MB 随机 pread
未命中页缓存中位 1.44 ms/p90 2.07 ms，顺序 4 MB 2.3 GB/s——NVMe 非
6.2 ms 有效单 miss 成本之源，~4 ms 管线开销（页缓存压力/单流同步/
chunk 互斥往返）待第三轮逐 miss 计时定位，详见
MOE_RESIDENCY_L2_PREDICTION_2026-10-01.md 新增节。

## 第一轮矩阵完成：六档 decode 22.0–29.8% 基线（全 FAIL）；四查门修复后重跑中（2026-10-01 04:50）

第一轮冻结全矩阵（23:46 二进制 4c5cddea，00:13–03:37）完成：基线 C=0
与候选 C=256+hot-256 均六档×3 全过，逐位一致 6/6（18/18 输出），目标档
token 合同 3/3（in=261887 out=257 finish=length，总上下文 262144）。
decode 比值 1024/4096/8192/45056/204800/261887 = 0.246/0.233/0.220/
0.264/0.298/0.229，全部 <50% 门槛（FAIL 如实记录）。内存峰值：基线
91.33 GB / 候选 58.49 GB（用户已接受超支）。候选加载量 261887 档
2891k/7.99 TB×3。根因不变：逐 miss 逐层串行 NVMe 补载
（evictions==loads，非热点槽池常满）。

四查门（verify-r2-binary.sh）第一轮跑（04:18，旧二进制 104c8b09）：
q4t_tests 106/106 PASS、bitexact-c256 两档 BIT-EXACT=True PASS、
affected-cancel PASS（mid-prefill/mid-decode 取消后同请求重发逐位一致，
槽位/状态恢复正确）；affected-fault FAIL——钩子按专家 id=401 布防而
401 是 layer 2 热点，InitHot 并行装载阶段触发钩子杀死启动
（"residency parallel stage failed"），curl rc=7。

修复（仅驻留层，GEMM 冻结未动；新二进制 b22a1049，04:43 构建）：
(1) 故障钩子新增 "first" 模式（Q4T_RESIDENCY_FAIL_EXPERT=first：首个
请求期 stage 失败）+ init_done_ 守卫（InitHot 结束前钩子不触发，布防
热点专家不再杀死启动）；affected-fault.sh 改用 first 模式。
(2) L2 recency 时钟与槽位 LRU tick 分离（l2_recency_）：L2 staging
不再推进槽位 tick，同调用内槽位 commit 不会压过 in-call 命中破坏
LRU victim 顺序。
四查门重跑（04:49 启动，setsid，新二进制，日志 verify-r2-binary.log）：
q4t_tests + bitexact + affected-fault + affected-cancel 全过才自动进入
第二轮矩阵。

L2 离线预测（MOE_RESIDENCY_L2_PREDICTION_2026-10-01.md，聊天轨迹
校准→留出 + 联合 C×L2 扫描）：第二轮 L2-128+C=256 预计 ~30% 基线
（L2-128 只接 ~6% GPU miss，仍低于 50% 门槛）；同内存下加 GPU 槽优于
加 CPU L2（C=384 无 L2，78.6 GB，~61% > C=256+L2-128，78.6 GB，~30%）；
验收夹具工作集 W≈270（第一轮 miss 率反推）明显小于聊天轨迹 349，
**C=324（68.4 GB，+14.4 GB 超支）在夹具上可能接近基线**——第三轮
首选候选。内存超支幅度（C=324/384）属新增超支，需用户明确授权后
才启动第三轮；NVMe 单 miss 6.2ms（≈450 MB/s）偏低，第三轮可并行做
逐 miss 时延分解/预取（GEMM 仍冻结）。GEMM 冻结用户再次确认
（"GEMM 暂时不修改,我们等当前目标完成后再讨论"）。

## 第二轮自动推进编排已就位；GEMM 冻结第三次确认（2026-10-01 02:48）

用户 2026-10-01 02:4x 第三次确认 **GEMM 冻结至本目标完成**（"GEMM 暂时不
修改,我们等当前目标完成后再讨论"）；第二轮范围（L2/钩子/常驻线程池）本就
不含 GEMM，无需调整。候选矩阵 261887 档 1/3（02:41），预计 03:45–04:00
完成。已部署 auto-r2.sh（setsid 常驻，日志 auto-r2.log）：等第一轮队列
结束 → verify-r2-binary.sh（q4t_tests 106 + bitexact-c256 +
affected-fault + affected-cancel）→ **严格四查门**（任一失败即停、不启动
第二轮矩阵、不自动重试）→ 通过才 setsid 启动 queue-r2-l2pool.sh（第二轮
全矩阵）→ 记录 r2 终态。verify-r2-binary.sh 补 pipefail 修复 rc 透传
（仅测试工具）。第二轮内存口径维持冻结计划：L2=128/层（+20.13 GB
pinned，启动即全额），预计峰值 ≈78.6 GB——超出用户已接受的 57.71 GB，
属新增超支，实测值将如实进入验收报告并单列决策（降 L2 选项：64/层
+10.07 GB、32/层 +5.03 GB，Q4T_MOE_L2_SLOTS 无需改码即可复测）。

## 262144 完整内存账已建立；候选矩阵 5/6 档完成（2026-10-01 02:30）

目标"先建立可复核完整内存账"完成：
[MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md](MOE_RESIDENCY_MEMORY_LEDGER_2026-10-01.md)
（预算侧逐项字节数+代码来源 + 实测侧 rss+gpu 权威口径 + 核对差异）。
关键数字：基线 C=0 实测峰值 91.33 GB（gpu 90.40）；候选 C=256 第一轮
58.49 GB（gpu 56.24，矩阵进行中已稳定）；第二轮 L2-128 池 = 48×128×
3,276,816 B = 20.13 GB pinned，预计峰值 ≈78.6 GB——超出用户已接受的
57.71 GB 量级，属新增超支，账内决策记录已标注（可降 L2：64/层
+10.07 GB、32/层 +5.03 GB）。swap 峰值 1.4 GB 单列，不掩盖超支。
候选矩阵 5/6 档完成（1024/4096/8192/45056/204800 各 3/3 逐位一致），
decode 22–29% 基线（<50% 门槛，根因逐步串行 NVMe 补载已定位）；
261887 档 02:13 启动，预计 03:30–03:45 完成。下一步：compare_e2e.py →
新二进制 GPU 验证（q4t_tests 106 + bitexact-c256 + affected 四查）→
第二轮全矩阵（queue-r2-l2pool.sh）。

## 分层专家驻留：第二轮冻结（L2+常驻线程池），矩阵 03:45–04:00 完成

用户 2026-10-01 再次确认 **GEMM 冻结至本目标完成**（moe_gemm.cu /
moe_decode.cu 等一律不动；驻留层/服务层/测试可改）。23:46 二进制候选
矩阵四档已确认 decode 22–27% 基线（<50% 门槛），根因是逐步、逐层串行
NVMe 补载；204800 档第 3 请求 prefill 中，随后 261887 档，预计
03:45–04:00 完成。矩阵结束后：compare_e2e.py 冻结口径对比 → 新二进制
（L2+钩子+常驻线程池，commit 35b9f7d 起）跑 q4t_tests（106 项）+
bitexact-c256 + affected 四查 → 第二轮全矩阵（同六档×3、同夹具、
--request-deadline-ms 10800000）。第二轮范围冻结见计划文档
"第二轮（2026-10-01）"节：L2 缓存（128/层，默认）+ 补载故障钩子 +
LoadPhase1 常驻线程池（消除每次补载块的 8 线程创建/回收，实测
0.173 ms/轮，decode 每步约 4 ms）；GEMM 未动。若第二轮仍有档 <50%，
第三轮候选：逐 miss 时延分解（NVMe pread 实测）、L2 容量调参、
C=324（内存约 66 GB，需用户再次确认超支）。CPU 微基准（无 NVMe/GPU，
矩阵运行中安全执行）：swizzle 双块 0.210 ms/专家（非瓶颈）、8 线程
创建+join 0.173 ms/轮。磁盘余 2.4 GB，持续观察。

## 分层专家驻留：L2 CPU 缓存与补载故障钩子已实现（2026-10-01 凌晨，矩阵运行中）

候选矩阵（23:46 二进制，4c5cddea）仍在跑（204800 档），GPU 验证待其
结束。本轮完成（仅驻留层/服务层/测试，GEMM 冻结未动）：
(1) L2 CPU 专家缓存——每层 pinned LRU 池（Q4T_MOE_L2_SLOTS 默认 128，
约 +17 GB 固定项，预算核算后 max_len=262144 不被 cap），命中快路径
免 NVMe 读，victim 跳过 in-flight/claimed，全忙同步等待；统计新增
l2h/l2m/l2ev/nvme_mb。这是冻结出口“CPU 二级缓存”的实现，供候选
decode 若 <50% 门槛时启用。
(2) 一次性补载故障钩子 Q4T_RESIDENCY_FAIL_EXPERT（仅测试默认关），
affected-fault.sh 两步合同所需。
(3) 新单测 residency_l2_cache_hit_evict。零警告构建；23:46 冻结二进制
封存于 bin-2346-fixed/；build/q4t 现为 L2+钩子构建（未 GPU 验证，
不得用于验收）。矩阵完成后：compare_e2e.py → 新二进制 q4t_tests
（106 项）+ bitexact-c256 + affected 四查 → 填验收报告。

## 分层专家驻留：修复二进制全矩阵重跑中（2026-10-01 00:13 启动）

23:46 二进制（sha256 4c5cddea，commit 509665d，工作区干净）按冻结轮
重跑全矩阵：基线 C=0 → 候选 C=256+hot-256，两配置统一
--request-deadline-ms 10800000（与冻结客户端超时 7200s/10800s 对齐），
六档×3，内存监控。runner 新增 --request-deadline-ms 透传
（tools/evalscope/run_acceptance.py，仅测试工具，无运行时改动）。
用户 2026-10-01 再次确认：目标档口径为**总上下文 262144**（非输入
256k），261887/261888 一字之差不再追究；C=256 每层按校准集命中次数
top-n，内存预算超支（实测 57.71 GB vs 54 GB）明确接受；GEMM 冻结至
本目标完成。旧 18:06 构建基线封存为 e2e-baseline-s0-1806-build，20:24
二进制候选部分结果封存为 e2e-cand-c256-2024-binary-partial，均保留。
首次启动（00:08）因进程组随执行会话回收而整体终止（服务端模型加载
中途消失，无 OOM/崩溃记录），改 setsid 完全分离后 00:13 重启成功。
磁盘余量紧张（2.9 GB），矩阵产物预计 <1 GB，持续观察。下一步：矩阵
完成后冻结口径对比（compare_e2e.py）→ 填验收报告；任一档 decode <50%
则按冻结出口评估 CPU 二级缓存或更大 C/预取，不放宽门槛。

## 分层专家驻留：decode 慢速 GEMM 回退与 20 分钟超时已修复，全矩阵重跑中（2026-09-30 深夜）

冻结轮首跑（20:24 二进制）：基线 C=0 六档×3 全过（目标档 3/3
in=261887 out=257，内存峰值 91.35 GB）；候选 C=256+hot-256 三档逐位
一致但 decode 仅基线 22%（50% 门槛 FAIL），45056 档被服务端 20 分钟
默认 deadline 取消后 runner 中止。根因：(a) 槽位模式 E==C≠512 使
decode 落到 host 编排分组 GEMM（约 4x 慢），与补载无关；(b) 服务端
默认 deadline 短于冻结客户端超时（7200s/10800s）。修复（仅驻留层/
服务层，GEMM 冻结未动）：槽位模式 decode 直调 MoEDeviceDecode（核按
常量 per-expert 步长寻址，E 无关；workspace 与 GEMM 路径逐项一致）；
新增 --request-deadline-ms（上限 10800000）；驻留统计分列
decode/prefill lookups/misses。验证：零警告构建（23:38 二进制）；
q4t_tests 105/105；bitexact-c256 1024/8192 均 BIT-EXACT=True；C=256
启动 3/3。ab-decode 中 20:24 二进制的 c256/c512full 启动段错误未复现
（当前二进制 3/3 OK），记为瞬态，矩阵中持续观察。目标长度档按用户
口径为**总上下文 262144**（输入 261887 + 输出 257），边界调查已关
闭。内存峰值（RSS+GPU 口径）：s0=91.33 GB，c256=57.71 GB——超 54 GB
门槛 3.7 GB，用户已明确接受（决策记录见计划文档）。下一步：同一
23:38 二进制重跑全矩阵（基线 C=0 + 候选 C=256+hot-256，均带
--request-deadline-ms 10800000，六档×3）→ 冻结口径对比。
验收报告骨架：
[MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md](MOE_RESIDENCY_ACCEPTANCE_2026-09-30.md)；
计划与冻结轮：[MOE_RESIDENCY_PLAN_2026-09-30.md](MOE_RESIDENCY_PLAN_2026-09-30.md)。

## 每层驻留名单（90%选择覆盖）已升级为校准→留出口径

2026-09-29：同样本事后口径升级为校准→留出：16唯一请求按(领域,长度)
分层8/8（请求7无decode，decode为8/7）；每(阶段,层)按校准频次排名、
留出定最小N。decode N90逐层144–324（和10387槽），prefill 136–287
（和10806）；两阶段共用统一容量取324/层=15552槽≈43.0 GB（slot
2,765,056 B，仅路由专家；共享专家/KV/状态/workspace另计）。留出N90
系统性高于同样本5–25%，部署容量应以留出口径为准，同样本仅作乐观
下界。逐层具体专家ID名单在
.q4t-work/moe-topn-90-20260929/expert_lists_90.json，作为下一步驻留
设计的静态基线候选；进入驻留设计前须以独立业务材料重校准、明确总
预算扣除项与补载安全合同。容量画像，非运行时收益证明；无模型/HTTP
运行。复现：tools/trace/layer_topn_heldout.py（2026-09-30入库，与封存
结果逐字节一致）。

## 主试验原始路由独立局部性分析已收敛（v2口径）

2026-09-29：v1把48层bitmask跨层OR造成专家ID碰撞，跨层工作集结论
作废；v2改逐层计算、跨层求和实例数（总空间48×512=24576）。单token
工作集48×10=480实例；逐层8/16/32-token窗口工作集45.2/70.4/104.5
(/512)，增长缓慢无饱和。时间局部性集中在≤8 token（lag-1重合为随机
基线2.3倍，lag-32回落到基线以下）；层间Top-32 Jaccard 0.040仅略高于
随机，跨层共享无结构红利；prefill/decode同层Top-64 Jaccard 0.116，
热点不可跨阶段复用。方向与回放/影子结论一致：收益空间在逐层小容量
LRU（32/64槽/层）。主试验原始路由已导出为可读文本（143M，gzip后43M，
27条目）供外部分析；导出与分析脚本均已入库。无模型/HTTP运行。报告
.q4t-work/moe-locality-20260929/REPORT.md（v2），证据
.q4t-work/moe-raw-export-20260929/。

## MoE分析工具收口（2026-09-30）

09-29分析脚本layer_topn_heldout.py与analyze_locality.py入库
tools/trace/（与封存结果逐项一致）；test_distribution.py（8项）与
test_hybrid.py（6项）接入公共host CTest，6项测试全部通过；三份MoE
专题文档（分层Top-N、混合分析、EvalScope多场景）补登docs/README.md
索引。公共host合同现为6项；distribution合同需要numpy（CI经apt安装
python3-numpy，本机优先.q4t-work/venvs/public-host专用venv）。无
运行时/模型/reference改动。

**默认入口已更新为`text-v1-moe-trace-20260928` / `3b414633`。**
用户已明确接受本轮MoE采集候选的实测性能代价。单Thor、文本greedy、
MTP/媒体关、max_seq=1；采集仍默认关闭，按需显式启用。
直接安装已验收二进制原件，2252个封存文件、运行时源码、发布包和构建缓存
身份复核通过，未重建默认静态库。安装后的1024-token精确输出/usage/SSE、
单槽健康与回收、正常停止通过；实际恢复旧98a75fb4后重新安装新版。
当前没有常驻服务。原质量/五档/失败专项按相同二进制身份复用，未重复矩阵。

包：.q4t-work/releases/text-v1-moe-trace-20260928/；默认身份见
build/q4t.release.json。部署证据与rollback-default.py位于
.q4t-work/moe-trace-deployment-20260928/；停止服务后可恢复旧默认。
成本接受：.q4t-work/moe-trace-acceptance-20260928/decision.json。
原测量、参考、候选封存时的待接受记录与旧发布包保留；本次接受决定
不改写严格持平未通过的事实，也不放宽以后通用门槛。本阶段关闭。

## 分层Top-N目标曲线已生成

48聊天输入、逐层分阶段完整N=1..512曲线；沿频率排名求达到目标的最小N，
不是最佳专家组合或运行时最优缓存。prefill达90%选择覆盖需171–321专家，
达80%整组概率需305–423；decode对应160–342、226–422。层0/17/47 prefill
90%选择目标分别312/321/171，层间差异明确。全曲线边界/单调/最小性和
与旧三档1,152项独立成员集合指标核对通过，无新推理。
完整48层表见[分层Top-N报告](MOE_LAYER_TOPN_2026-09-28.md)。

## 同口径Top-256集合分布已补齐

相同48聊天输入，prefill/decode选择覆盖87.29%/92.37%，每次top10平均落入
8.729/9.237个；全部十个落入概率43.50%/58.45%。源摘要、频次、概率界限及
Top128到Top256单调性核对通过，无新推理。这是分层同样本集合分布，非
LRU缓存命中或全部层同时覆盖；详见[分布报告](MOE_DISTRIBUTION_2026-09-28.md)。

## 同口径Top-128集合分布已补齐

相同48聊天输入，prefill/decode选择覆盖67.60%/71.42%，每次top10平均落入
6.760/7.142个；全部十个落入概率14.76%/13.69%。原始路由精确计数与频次、
源摘要及Top30集合单调性核对通过，无新模型执行；不是缓存泛化评估。
完整逐层表与专家概率见.q4t-work/moe-top128-20260928/及[分布报告](MOE_DISTRIBUTION_2026-09-28.md)。

## 每层Top-30集合分布已导出

48聊天输入、同样本分阶段逐层选Top30；prefill/decode选择覆盖33.41%/30.76%，
每次实际top10平均落入3.341/3.076个，全部十个都落入的概率0.653%/0.0974%。
各层专家单独概率和0–10个重合分布已保存；从原始token路由精确计数并匹配
频次向量，无独立性假设。属于描述性统计，不是新请求缓存评估，无新模型
运行。入口top_expert_sets.py，详见[分布报告](MOE_DISTRIBUTION_2026-09-28.md)。

## 专家进入top-10的逐层出现概率已导出

按用户明确口径，以token路由次数为分母，导出48聊天输入的prefill/decode
全部512专家概率及每层最高10位。概率等于原单专家选择份额的10倍，各层
概率和为10；不是联合概率或缓存命中。层47 prefill专家157为92.42%，
decode最常见专家442为23.90%。全部层/专家与源摘要核对通过，无新推理。
入口expert_probabilities.py，详见[分布报告补充](MOE_DISTRIBUTION_2026-09-28.md)。

## MoE专家ID分布统计完成（与缓存命中分开）

当前同二进制/模型的9批139请求核验，59次同输入重复的完整路由一致；分组
去重后80输入，其中48聊天材料为主分析。已提交ID共1,568,887,680个。
prefill/decode每层平均活跃511.48/473.23专家，Top128占比67.60%/71.42%；
两阶段同层Top64名单仅重合18.59%。decode同场景请求名单重合76.04%，
跨场景35.08%，模板/长度混杂使其不能被解释为语义专用性。逐层、逐专家、
块/请求出现率、等权/路由权重、前后半段及场景/长度分组均保存。
384个历史阶段/层完整向量交叉核对一致，全部分组另行重算；8项合同首轮
7通过，失败forward计数边界修复后单独重验通过。无新HTTP、缓存模拟、
吞吐或常驻名单结论。下一步用独立业务材料验证分布稳定性。
详见[完整分布报告](MOE_DISTRIBUTION_2026-09-28.md)，证据.q4t-work/moe-distribution-20260928/。

## 固定热点+LRU初步分析完成

复用24条轨迹，64/128/256槽比较固定0/25/50/75/100%，同预算、旧冻结排名，
无未来训练。跨请求保留时所有非零固定比例的汇总总加载均比纯LRU更多；
每请求冷重置时256槽固定192+动态64的总量少约9.2%，18/24改善、最短六条
更差。固定填充已计费；仅本批探索性候选，不认定统一最优比例。6项直接
合同、4,432,320次组/策略检查及288条端点匹配通过，无新HTTP或运行时变更。
下一步按生命周期区分策略，并以独立业务留出请求验证；见[混合分析](MOE_HYBRID_2026-09-28.md)。

## 同轨迹128/256槽容量扩展完成

复用24条EvalScope轨迹离线补算四档容量，无新HTTP；2,363,904项需求组/策略
独立核对通过，32/64逐请求统计与原结果完全一致。128/256槽连续LRU选择命中
83.73%/94.57%、整组33.11%/66.61%，预算17.016/34.005GB，仅路由专家及staging。
256槽每请求重置时，最长六请求总逻辑加载高于静态：冷prefill开销抵消decode
改善。因此将128/256纳入离线容量权衡，需结合冷启动与跨请求保留；没有
实际加载/驱逐或吞吐证明。原质量/成本边界不变。见[容量补充](MOE_EVALSCOPE_SCENARIOS_2026-09-28.md)。

## EvalScope新增多场景采样完成

按用户要求新增六类任务各4条，共24条EvalScope单流请求，实际输入114–13,607
token、输出均128截断。24/24 HTTP正常，18,432项逐层独立重算一致；保存
逐请求路由/JSON、原始数据库及场景CSV。32/64槽、两模式均24/24减少decode
缺失和总逻辑加载；64槽连续模式静态/LRU选择命中32.48%/69.18%，LRU整组
全命中13.21%。保留64槽LRU候选，尚无真实补载收益证明。合成相关样本、
截断输出不代表生产分布或质量通过；旧性能不持平结论不变，未重复五档。
运行时/默认部署未变，服务已停止。详见[多场景报告](MOE_EVALSCOPE_SCENARIOS_2026-09-28.md)。

## MoE 请求完成边界影子观察已交付，严格持平未通过

用户确认减少专家常驻内存、尽量保持性能的方向后推进；按最新约定先收敛
工具/采样/分析，再补最终验收，已通过且身份未变的证据复用，不反复测试。
独立旁路消费原子发布的完整请求，32/64槽静态/LRU、prefill_reset/continuous，
统计全部已提交decode；不提供逐层及时可见性，不控制权重。运行时及默认
部署仍为3b414633，124项源码/二进制/检查器与已接受包一致。

13项影子合同、既有13项回放合同通过，无SKIP；质量11/11，两个完整五档
各15请求均匹配冻结输出，新材料8条HTTP/SSE正常。34份影子轨迹26112项
逐层统计与独立重算一致，41条evalscope原始记录及8份SSE核对通过。
新8请求（5个对话）两容量/两模式均减少decode缺失和总逻辑加载；64槽
连续模式选择命中70.65%、整组全命中15.52%，不换算吞吐或实际内存节省。
静态新材料迁移弱、固定顺序、21K而非200K长文及128输出上限限制外推。

最终开启影子相对同二进制采集on/影子off，8K decode均值−0.244%，后两次
落在对照范围较慢一侧，严格持平未通过；不追加采样，不自动接受新增成本。
观察器自身RSS峰值约24.2MiB，积压峰值1，最多15.28秒才完成请求后统计；
不证明补载及时。仅按需诊断交付，默认不开启；真实加载/驱逐和预取均未做。
范围、成本、复现与边界见[影子报告](MOE_SHADOW_2026-09-28.md)，
证据.q4t-work/moe-shadow-20260928/。后续保留64槽LRU候选，先明确实际容量
及补载/复用安全合同，不自动进入真实驻留管理。所有本轮服务已停止。

## MoE 固定容量离线回放已交付

静态热点与整组LRU、每层32/64/128/256槽、三种重置/保留模式完成。
8条校准、8条留出、5条既有独立五档输入；源117文件与原封存匹配。
13项缓存合同、15项来源检查、3项CLI拒绝通过；4992组冷decode独立重算
一致。工具与合同接入host，默认运行时/二进制/模型/reference未改，无新HTTP。
32/64槽LRU在13条请求、三模式的decode均减少缺失；但长prefill在逐请求
重置时可抵消改善，128/256槽不稳定优于静态。建议仅32/64槽进入有界在线
影子统计，优先64；当时限定离线，后续请求完成边界影子结果见上文。预算含scale、对齐与10槽staging，
不把覆盖或逻辑加载转换为吞吐。合同、限制、逐请求分布与复现见
[回放报告](MOE_CACHE_REPLAY_2026-09-28.md)，证据在
.q4t-work/moe-cache-replay-20260928/。未来真实驻留仍需容量实测与独立验收。

## MoE Router 首轮真实采集观察

2026-09-28在默认3b414633上显式启用采集：主试验四类材料、两个种子、
约1K/8K，16条唯一聊天请求+4条精确重复；每条输出128 token，统一分析
前64次decode。20份轨迹完整，共38695680个专家ID，4/4重复文本和路由
完全一致。裸prompt试采另20条保留，不与聊天、关闭思考的主试验混算。
每层512专家中固定训练top128，对8条留出覆盖82.1%；对同二进制既有五档
独立输入覆盖70.4%–76.7%。同请求prompt top128对留出decode仅覆盖39.6%。
相邻top-10集合重合38.5%，同窗口随机不同token对为21.0%；存在时间局部性，
但各层9.4%–59.9%差异很大。全部覆盖指专家选择次数，不是权重贡献、cache
命中率或SSD收益。共享模板、每类仅两个种子，不能把任务/语言差异解释为
语义专用专家，也不能把长度/内容共同变化当因果。独立重算与分层图核对通过。
证据和复现脚本：.q4t-work/moe-router-study-20260928/findings.md。
后续固定容量回放现已完成，结果见上文；此首轮观察未实现缓存或offload，
未运行新性能验收。测试服务已停止，
默认二进制、部署元数据与既有参考均未改；采集仍按需显式开启。

## MoE 受控采集已交付，实测代价已接受

实现2531988/main，默认版本3b414633；本机发布包：
.q4t-work/releases/text-v1-moe-trace-20260928/（q4t与检查器、源码和摘要）。
在仓库根目录按[轨迹工具](../tools/trace/README.md)显式启用；默认关闭，
仅单序列文本greedy、MTP/媒体关。已覆盖scheduler、chunked prefill及
inline/fallback。阶段、位置、层号来自实际入口与ModelSequence，未改Router
算法或同步策略。8192 chunk固定15 MiB device + 60 MiB pinned；后台编码
写盘，队列满/配额/IO失败停收并保持incomplete。OOM禁用采集并清理部分
池；其他CUDA错误保留服务失败语义，最终drain失败隔离缓冲至进程退出。

保存实际输入token、HTTP ID、请求终态、逐层专家ID，以及二进制、模型
index/config、命令/相关环境和工作负载摘要。离线工具校验来源与完整性，
输出prefill/decode频次和同样本事后覆盖；不推导缓存收益或吞吐。
普通文件IO仍可能阻塞关闭，未提供故障文件系统硬超时或长期采样保证。

Thor零警告构建、46项host通过；公共CI的42项host、49项格式、16项框架
反例、4项证据审计和15项来源分析检查通过，无SKIP。
[代码CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/36385196276)。
最终候选off/on质量各11/11，旧默认/off/on各完成五档、每档3次、输出256
Token，全部匹配冻结参考。26份完整轨迹共519929760个专家ID，96个层/阶段
统计组。67条evalscope原始记录及9条直接生命周期SSE审计通过。
取消8组、decode失败4组、生命周期5组、prefill读回失败2组通过。
采集专项15项：12项在4bc183a3通过，按未变producer/worker/同步与调度
代码身份复用；最终候选补跑device/pinned OOM与manifest close失败3项。
55项输入/63项模板证据按未变实现复用。106个设备函数与上一候选一致；
相对旧默认98a75fb4仅既有GDN变体，精确cubin对复用60例有限数值证据，不宣称整模型
数值oracle或设备代码全部与默认相同。初版与修正前证据保留，不混用身份。

下表默认指测量时旧版98a75fb4；decode为调和均值，TTFT为算术均值。
原始三次及首请求/后续范围另存。

| 上下文 | off相对默认 decode | off相对默认 TTFT | on相对off decode | on相对off TTFT |
|---|---:|---:|---:|---:|
| 1K | +0.032% | -0.278% | +0.122% | +0.044% |
| 4K | -0.004% | +0.023% | -0.050% | -0.212% |
| 8K | -0.167% | -0.157% | -0.140% | +0.037% |
| 44K | -0.011% | -0.093% | +0.028% | -0.070% |
| 200K | +0.374% | -0.074% | -0.184% | -0.032% |

关闭态1K/4K TTFT、4K/8K decode的后续请求落在旧默认重复范围较慢一侧，
严格持平未通过，未追加有利采样。用户现已明确接受本轮实测性能，
3b414633已安装为默认；接受决定见上文，旧测量保持原样。开启态与关闭态
的后续范围重叠，也不据此宣称零开销；采集继续按需显式启用。
本阶段交付结束；专家offload、缓存执行器、长期采样均未做。
证据：.q4t-work/moe-trace-runtime-20260928/audited-results.json，
同目录保留原始HTTP、轨迹、故障注入、复用身份与候选包校验。

此前host格式基础已纳入本功能；早期仅合成样本的证据保存在
.q4t-work/moe-trace-foundation-20260928/，不替代上述真实运行记录。

## 此前入口治理：已完成并接受实测代价

实现e58e3f2已推送main，无额外分支；98a75fb4已替换默认d9d02f92。
ServerOptions是唯一配置源，能力组合由它派生；裸serve默认max_seq由8
改为1，MTP默认关、媒体默认关，显式开关与组合保留。严格解析整数与
有限比例，非法数值、范围、缺参在加载前退出2，不再静默截断或回退到8。
启动报告请求/实际能力；关闭媒体不加载vision，显式媒体仍加载原视觉模型。
保留原预算算法、max_len自动容量及max_prefill默认语义；裸入口启动时
预算给出262144容量，只验证了1024-token文本，不新增262K质量接受结论。
正式HTTP矩阵仍固定8192/208896，与原版预算报告逐项相同，不用省下的
视觉资源扩大容量。没有第二套runner、JSON配置或生成/调度/模型改动。

Thor零警告构建、34项host、14类实际CLI拒绝通过，无SKIP。默认及显式
媒体两种实际启动、单槽、精确文本SSE、正常停止通过；默认媒体拒绝400。
显式媒体仅检查初始化与文本，不代表媒体生成验收。公共CI通过30项host、
16项框架反例及4项审计检查。
[实现CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/36373380858)。
HTTP质量11/11、55类输入拒绝/恢复、取消8组、decode故障4组、prefill
读回故障2组、生命周期5组通过。41条evalscope原始记录、9条生命周期
SSE和2条入口SSE审计通过。模板/输入/生成源码身份相同，复用此前63条
模板专项；没有重跑未受影响的全部专项。106个GPU函数有1个GDN机器码
变体，实际新旧cubin精确匹配此前已验证的两份cubin（方向相反），复用
60例有限数值对照，不宣称设备代码全部相同或完整模型oracle通过。

候选98a75fb4与当时默认d9d02f92各一轮完整五档、每档三次、输出256token，
均与冻结参考一致。
下表decode取调和均值、TTFT取算术均值；首请求/后续请求另存原始证据。

| 输入token | Decode变化 | TTFT变化 |
|---:|---:|---:|
| 1024 | -0.416% | +0.171% |
| 4096 | +0.002% | -0.175% |
| 8192 | -0.081% | +0.061% |
| 45056 | -0.105% | -0.016% |
| 204800 | +0.160% | +0.080% |

1K及44K后续decode在较慢方向超出本次原版重复范围，未满足严格持平
出口；不追加采样，不修改参考，不从减少视觉加载推导吞吐。一次先候选
后原版不证明因果或稳定尾延迟。用户明确接受本轮实测代价，据此安装
已验收原件并关闭待决项；未满足严格持平出口的测量结论保持不变。
发布包已核对117项运行时源码、二进制及构建缓存，已激活，无常驻服务。
证据：.q4t-work/serve-entry-20260928/audited-results.json；同目录package/
为候选包，artifact-binding.json封存原始材料。未做：MoE采集/offload、
独立CLI治理、MTP/媒体生成验收、长稳复验、完整实验执行器拆分。

## 当前维护增量：两项改造完成，实测代价已接受

实现365f1f2已推送main，无额外分支；已接受d9d02f92并替换默认45df506f。
只推进Model设备内存owner和ChatServer职责拆分。ModelOwner不可复制/
移动，失败加载检查完成后释放部分资源，拒绝覆盖已加载模型；服务
停止并释放MTP借用后由owner释放Model，补上原服务缺少Model释放的责任。
独立CLI/历史实验调用者仍保留原API，未宣称全仓所有权迁移完成。
ChatServer拆为生命周期、HTTP、输入准备、调度、生成和指标实现文件；
入口2354→335行，生成882行，保留共享状态、锁、调用顺序与默认配置。
逐函数核对仅加载入口替换及输入准备提取；106个GPU函数机器码相同，
复用既有数值证据。无kernel改动、异步回收、Graph或实验分支隔离。

Thor零警告构建、30项host、TCP三类连接合同通过，无SKIP；公共CI
26项host、16项框架反例、4项证据审计通过。真实CUDA owner专项覆盖
空对象、部分加载失败/重试、分配失败、拒绝覆盖已加载模型；第40及
1500次分配失败均无存活分配，两次完整加载各1538次分配只释放一次。
HTTP质量11/11、模板63条、取消8组、decode故障4组、prefill读回故障
2组、生命周期5组通过。104条evalscope原始记录及9条正常SSE复核通过。
首次缺少网络声明头的构建失败保留；修复后运行时与验证期间身份未变。
[实现提交CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/36367178347)。

候选d9d02f92与当时默认45df506f各完成一次五档矩阵，每档三条、输出256token，输出均与
冻结参考一致。下表decode为三次调和均值变化，TTFT为三次算术均值变化；
首请求与后续请求分别记录，没有追加采样或修改参考。

| 输入token | Decode变化 | TTFT变化 |
|---:|---:|---:|
| 1024 | -0.083% | +0.430% |
| 4096 | +0.018% | +0.078% |
| 8192 | -0.038% | +0.161% |
| 45056 | +0.001% | +0.079% |
| 204800 | -0.109% | -0.041% |

1K/4K/8K后续TTFT、8K后续decode在较慢方向超出原版重复范围，故未满足
EVALUATION的性能持平出口；一次候选/原版对照不证明因果或稳定尾延迟。
用户随后明确接受本轮实测性能代价，据此关闭待决项并更新默认；上述
未满足严格持平出口的测量结论保持不变，未重复五档或修改参考。
发布包与115项运行时文件、二进制及实际构建缓存一致；部署检查见上文。

完整证据：.q4t-work/server-owner-20260928/audited-results.json；同目录
package/为候选包，artifact-binding.json为封存清单。未做：实验路径
隔离、MoE采集/offload、独立CLI迁移、媒体/MTP验收、长稳复验及独立
关闭scheduler的HTTP fallback验收。后续入口治理见上文；此处仅记录此前所有权与拆分的验收。

## 原首版收敛记录（历史e659a108，不代表当前默认）

**首版私有文本Runner已完成收敛并安装默认入口。** 版本
`text-v1-20260928`，二进制e659a108；单Thor、指定模型、greedy、
MTP关闭、max_seq=1，五档1K/4K/8K/44K/200K。范围和完整证据见
[首版收敛验收](RELEASE_TEXT_V1_2026-09-28.md)。

成组资源修复与发布工具交付：上传绝对期限、正文与在途预算、
独立控制代理、校验包、监督重启、升级及失败回滚。22项主机、
55类输入拒绝/恢复、11题质量、63条模板、15条五档性能、8组取消、
4组decode故障、10项生命周期/资源检查全部通过；连续30.03分钟
112条请求通过，RSS峰值增量12.55MiB。89条evalscope原始记录与
121条正常SSE复核，1167文件封存。测试脚本一次失败保留，
仅修复并重跑服务阶段，运行时/配置未变。

已替换含已知缺陷的旧默认b5f38e48，默认构建树已同步；发布包
`.q4t-work/releases/text-v1-20260928/`可激活，当前没有常驻服务。
五档decode相对历史变化−0.164%～+0.214%；这是必要修复的代价记录，
不声明严格零回退，旧8K ABBA−0.277%结论保留。正式参考已更新，
旧参考归档。模型/量化源码与设备代码未变，明确复用既有数值证据。

本轮固定清单已关闭。媒体/MTP、公网多租户、认证/TLS与业务SLO、
完整独立模型oracle及offload/数据流属于其他交付范围，不再延长
本轮收敛。后续方向列于文末，不表示当前已验收合同仍在待办。

2026-09-28阶段交接复核：发布包、当前src/include、默认二进制、
性能参考及1167个证据文件身份一致；已修正过期文档入口，未改运行时
或重跑推理。可从此版本进入下一阶段，详见
[基线交接](BASELINE_HANDOFF_2026-09-28.md)。

## 本轮维护结论：功能通过，实测治理成本已接受

2026-09-28：用户已接受`45df506f`的本轮实测治理成本，维护阶段验收
关闭；PR #1已并入main（a115cb4），已合并工作分支已清理。
不将其表述为性能持平，也不放宽后续改动的通用门槛。按用户后续授权，
已安装`text-v1-maintenance-20260928` / `45df506f`，正式参考保留。
此前基线交接是维护前快照；当时主线源码与该部署包的运行时摘要一致；当前增量见上文。

普通文本以ModelSequence为唯一提交状态，区分submitted/committed；
删除serve重复游标、PLE历史及prefill状态重建。默认模型序列API等待
检查完成后提交；serve显式deferred，复用锁内读回同步。失败可见，
未完成工作拒绝结束/重入；model_mu_与默认流同步保留，无Graph。
代码中没有JSON驱动的模型执行计划；未新增平行计划源。删除旧
EXECUTION_PLANS.md的282行提案及唯一索引，其他提案仅作历史材料。

公共Linux CI实际通过26项host合同、16项框架反例、4项封存审计检查，
无CUDA或私有夹具依赖；Thor发布30项（原22项加8项）通过，无SKIP。
封存工具从冻结清单和真实PASS取证，不再硬编码22项。空筛选与SKIP
拒绝规则保留并实际运行。最终构建零警告，现有测试调用者编译通过。
[公共CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/36343286134)。

本机Thor、同一候选：序列提交/完成/失败/重入/回收与同分块交接通过；
HTTP质量11/11、取消8组、decode故障4组、prefill读回故障2组通过；
生命周期5组、9条正常SSE通过，两轮并发输出等于顺序输出。
候选与原二进制各完成五档、每档三次、256输出token，共30条性能
请求；输出均与冻结参考一致。原始41条evalscope记录和9条正常SSE
复核通过。模型目录、reference/、kernel源码与MTP算法未改。

下表为本次候选相对本次原二进制的完整HTTP对照：decode取三次调和
均值，TTFT取算术均值；首请求/后续请求分别保存在证据中。

| 输入token | Decode变化 | TTFT变化 |
|---:|---:|---:|
| 1024 | -0.267% | +1.154% |
| 4096 | -0.135% | +0.123% |
| 8192 | -0.286% | +0.035% |
| 45056 | -0.044% | +0.124% |
| 204800 | +0.123% | +0.094% |

1K/4K/8K后续decode和1K/44K/200K后续TTFT在较慢方向超出本次参考
重复范围，**不满足EVALUATION的性能持平出口**。没有追加挑样本、
改参考或用长档收益抵消短档回退。一次候选后一次参考不能证明因果
或稳定尾延迟，未把变化归因于同步次数。用户随后明确表示“我可以
接受这个部分的治理成本”，据此接受该候选的已测代价并关闭待决项；
这是本轮特定接受决定，原始未满足持平条件的测量记录保持不变。

首轮新序列夹具混比全行/末行head失败，保留状态0差、logits171差
原证据；仅改成同形比较后通过，未据此修改运行时。新构建106个GPU
函数中1个GDN函数机器码不同（源码和构建选项相同，原因未确定），
因此未宣称设备代码完全相同。对两份二进制提取的实际函数做60例
有限域对照：327366144个输出、25067520个状态值逐位一致且无非有限值；
不扩大为完整模型oracle。首次构建警告及修复后的零警告记录均保留。

实现提交140bee6；验证期间105个运行时源码摘要无漂移。证据与逐次
性能数值：.q4t-work/runner-maintenance-20260928/audited-results.json，
同目录保留首次失败、命令、二进制/源码、原始响应和最终封存清单。
未做：完整D/P/S执行器、GRFrame闭环、异步回收、kernel重写、媒体/MTP
验收、维护版长稳复验；未独立覆盖关闭scheduler的HTTP fallback。
默认文本路径行为回归通过；本轮接受依据为上述验证及用户的成本
接受决定，不是提交/推送本身。决定另存
.q4t-work/runner-maintenance-acceptance-20260928/decision.json，
绑定原1117文件封存清单与候选摘要；原证据包未改写。
部署单独验证发布源码快照包含cmake/共享清单。首次停止探针误用
端口bind而报EADDRINUSE，保留原失败；改验进程退出和连接拒绝后
冒烟通过，运行时未改。未重建默认静态库，直接安装已验证包二进制，
build/q4t.release.json指向其真实构建缓存；未把新构建冒充已验收版本。

## 历史阶段结论（不代表当前部署或下一步指令）

文本服务合同与主机门禁202794eb已完成有界功能回归，但性能无回退
验收未通过，默认二进制b5f38e48及参考保持不变。未实现参数明确400，
媒体默认在解码前拒绝，89处跳过改为真实SKIP；16项框架反例、21项
主机门禁、55类HTTP拒绝/恢复、11题质量、63条模板及4组decode故障
通过。五档15条输出同参考，decode较历史低0.001%–0.082%；追加
一次8K ABBA发现decode低0.277%，按首/后续请求分层范围不重叠，
不能以总体范围重叠判无回退。101条原始HTTP复核、510文件封存。
GPU代码包逐字节相同，下一节点限定定位host/调度/构建布局相关的
8K差异，不重复选有利样本或恢复错误计算。详见
[服务合同与验收](SERVICE_CONTRACT_2026-09-27.md)。

前置修复已进入上述候选：[decode错误终态](DECODE_FAILURE_2026-09-27.md)
及[JSON/HTTP输入边界](INPUT_BOUNDARY_2026-09-27.md)分别保留140/179
文件的独立直接回归证据。旧默认部署仍含已复现缺陷，不代表生产可用；
源码修复成立、性能接受与商业发布必须分开判断。[完善度审计](PROJECT_READINESS_REVIEW_2026-09-27.md)
中的全局主机/媒体资源预算、统一升级回滚与业务SLO仍未交付。

独立控制入口已完成有界交付：数据/控制双Nginx进程分别拥有连接
预算；共享进程128连接对照的健康请求失败，双进程三轮耗尽中
15次健康及3次实际取消全部低于固定2秒门槛。数据reload、双侧
独立重启、200K输出通过，8条正常SSE审计、102文件终态封存。
这是可选双端口配置，未自动启用、未改b5f38e48或性能基线；没有
双进程长稳或生产P99证明。详见[独立控制报告](ISOLATED_CONTROL_2026-09-27.md)。

控制预算隔离与有界耐久验收完成：原共享预算导致监控慢请求阻塞
取消429的反例两次复现；本地Nginx模板已分离生成4/取消8/健康4/
指标4。指标满额仍可取消实际请求；取消自身满额保留429，空闲
上传超时后恢复。17轮/628.6秒混合负载、活动请求停机与重启恢复、
新配置200K代理回归通过，39条正常SSE审计、215文件封存。
未改b5f38e48二进制/性能基线，未启用公网服务；十分钟不是生产
长期稳定性或控制可用性SLO证明。详见
[控制预算与耐久报告](CONTROL_BUDGET_2026-09-27.md)。

后端监听b5f38e48已交付并部署：默认127.0.0.1，--host数值IPv4
显式开放指定接口；配置错误在模型加载前拒绝。三种真实监听、
七项错误配置、显式loopback代理矩阵、11题质量及五档15条输出
通过，211文件终态封存。decode较432fc489历史低0.02%–0.36%，
五档TTFT/decode范围重叠，非严格零回退证明。默认远程访问行为
有意改变；同机进程隔离不在此合同内。详见
[监听地址交付](LISTEN_ADDRESS_2026-09-27.md)。

真实Nginx代理准入阶段已完成有界验收：生成预算满额时超额429，
健康/取消仍可用；慢上传隔离、FIN/RST与首内容后断连、200K完整
输出和3次恢复通过。裸后端128连接满载取消503的限制实测保留。
本轮仅增加配置/验证工具，未改432fc489运行时或启用公网入口，
未测五档代理性能。下一步后端私有访问合同，再做长稳/恢复。
详见[代理准入报告](PROXY_ADMISSION_2026-09-27.md)。

请求级取消432fc489已交付并部署：排队/prefill/decode统一取消，
支持独立凭据、期限及FIN/RST，完成/取消仲裁和关闭安全回收。
直接竞争/TCP、完整HTTP取消矩阵、copy/sync与fatal accept故障
注入通过；固定11题和五档15条全文同参考，26条原始记录审计封存。
decode较2319历史变化−0.004%～+0.240%，五档TTFT/decode范围均重叠，
未检测到超出已测波动的回退，不证明严格零回退或工业级发布完成。
EOF现在取消请求，不再支持写半关闭等待结果；下述旧FIN限制为历史。
详见[请求取消交付](REQUEST_CANCELLATION_2026-09-27.md)。

长prefill确认连接失败的取消修复已部署2319b14a：RST在8192/45056
处结束，完成已提交GPU工作后释放槽位；直接TCP、9条生命周期
响应及固定11题HTTP通过。五档各3次性能采集完成，全文同参考；
decode聚合速度较历史低0.21%–0.39%，范围重叠但非同时段配对，
不声明严格零回退。普通FIN仍不能与合法写半关闭区分，MTP未实测。
详见[取消修复](PREFILL_CANCEL_2026-09-27.md)。

有界状态验收已收束：新增257-token、127/1/128/1同分块直接检查，
五个交接点状态/全词表末行logits逐位一致，history/position/stage、
有效文本RoPE、页表/缓存未写区与槽位隔离通过。无生产变更，零警告，
退出0。覆盖矩阵及未覆盖边界见[状态交付](STATE_ACCEPTANCE_2026-09-27.md)。
取消修复现已独立交付；不冒称整模型或严格性能无回退验收完成。

当前服务生命周期补验：1K/44K的A→B→A、两轮两槽位重叠、
首响应前及首内容后断连恢复通过；9条完整HTTP响应的文本、usage、
stop一致，槽位归还。copy/sync读回错误注入均500、释放槽位并
将服务置为不健康，后续503；终态证据已封存。未改生产代码/部署，
未重测性能；输出一致
不等于logits逐位证明。详见[生命周期验收](STATE_LIFECYCLE_2026-09-27.md)。

最新状态合同验收发现并修复序列API默认槽位写错：Begin绑定槽位1，
旧prefill/decode省略参数却访问槽位0。修复统一使用序列槽位并
拒绝冲突/越界；完整模型短输入状态/缓存/logits回归及复用检查
通过。HTTP固定11题全文同参考，11条原始记录与89文件已封存；
修复已部署。本构建未测五档性能，不声明性能无回退。详见
[槽位修复](SEQUENCE_SLOT_FIX_2026-09-27.md)。server现有调用显式
匹配槽位，不能据该API反例声称线上请求已经污染。


**完整E4M3编码修复已合入并部署，建立新的数值/性能基线。**
合成与捕获激活各341600组有限域接入测试：尺度、FP4和decode
alpha差异均0；检查点147456个标量正有限、49152对gate/up相等。
干净版26题全文同归档修复版：检索11/11、推理13/15。五档各三次
HTTP性能采集通过，67条原始记录审计与315文件终态封存完成。
详见[修复接入报告](E4M3_NUMERICAL_FIX_2026-09-27.md)。

历史兼容版独立推理14/15，原4K求和输出600440；修复纠正该题，
同时1K/44K两题变错。历史新120题82→80与5个损失复现保留，
但不能以语义分数否决已证明的编码修复或恢复错误计算。
本次接受限于已测格式/接入合同；未证明整模型数值正确或商用发布
就绪，性能也没有同时段旧版对照，不宣称严格零回退。

历史MoE路径已确认[248,432)编码为448及次正规舍入/进位缺陷。
原4K输入/中间量化分别有1208940/148339项高区间错误，覆盖48层。
本次修复共用编码函数；旧路径的条件矩阵乘一致不能抵消其尺度错误。

此前交付（2026-09-27，以下为当时判定，最新验收见上）：停止逐层扩展，完成两个MoE量化点
HTTP隔离及直接格式回归。原编码147项失败、仅高值27项失败，
此前完整修复33146项全部一致，形成可独立复现补丁包。两个新
运行时候选均新增1K错误且4K仍错，均不接受；已恢复原版本并
核对三题HTTP。编码修复可验证，默认部署仍未通过质量要求。
详见[有界交付结果](CORRECTNESS_DELIVERY_2026-09-27.md)。
此前离线传播止于layer2注意力后主干，证据保留，停止继续铺开；
见[尺度传播](MOE_SCALE_PROPAGATION_2026-09-24.md)。

此前完整QSA选择/attention指定算术相同；高精度保留概率与门控
BF16舍入后仍有175500项差异。以上均为实际输入上的条件参考，
不是从原始token独立生成整模型结果。两轮日志清单生成时机问题
已修正并保留原证据，后续均在写入进程终态后封存。

## 目标与工作规则

先完善runner，以保持精度、各档性能不回退为门禁，再逐步演进
模型专用数据流引擎。性能持平且净复杂度下降也接受；文档、封装
或生命周期缩短不能直接当成已测吞吐/系统峰值容量收益。

用户已授权自主推进、修正、回退及阶段commit/push main，无需
常规请示。模型目录与reference/只读，保护其他任务未提交内容。
每次有意义的改动更新本入口并向当天日志顶部追加，不改日志历史。

- 性能优化必要构建后先tools/evalscope真实HTTP，E2E通过后才做
  数值/计时/profile，不用旧bench。Bug修复可先用直接针对缺陷的
  单测、边界或差分；实现符合性与HTTP任务质量分开判断。
- MTP关闭，单流greedy，max_prefill=8192、max_len=208896。
  输入1024/4096/8192/45056/204800，每档固定同输入三次、输出256。
- TTFT包括HTTP/分词/prefill，不能称纯prefill时间。按重复范围比较，
  不临时加容忍百分比、不拼接最优档位、不反复采样寻找通过。
- 只读观测的首项为关闭/开启/关闭原4K HTTP：原始请求、全文、usage、
  stop及服务退出一致才继续定位。原错答保持只证明观测不变性；
  质量驱动退出1仍是质量失败，不能写成模型验收通过。
- 已绑定的冻结HTTP证据允许离线参考复用；不为纯离线分析重跑HTTP。
  新实验新目录，逐份绑定来源，全部差异保留，明确参考的设备依赖。
  存储规则见 [EVIDENCE_STORAGE.md](EVIDENCE_STORAGE.md)。

## 历史运行时与质量边界（截至2026-09-27）

本节至“历史性能参考”的旧部署、执行节点与数值记录保留作历史说明；
其中b5f38e48不是当前基线。当前以本文顶部、首版发布/交接报告及正式
performance_reference.json和metadata为准。

当前部署为请求取消与监听地址修复，build/q4t SHA256：
`b5f38e48ae9357e73d415d24ce5307038da5fa39f9eb297ad3678056c2dd3376`。
默认loopback，远程监听须显式--host。该二进制通过实际监听、代理
回归、固定11题及五档各三次性能；详见监听报告的测量边界。
旧432fc489及更早二进制/参考保留；默认build静态库尚未同步，
后续开发仍需必要构建。没有启用常驻服务或修改防火墙。



- 入口修复：固定11/11、独立14/15且独立15条全文同正式；永久入口
  HTTP工具63条及模板字节21/21通过。真实messages五档相邻45条
  长度/全文一致，三服务0，完整三次范围无不利分离。后两次仍有
  单侧不利项；未发现同时差于两侧参考，不证明严格零回退。
- E4M3合并修正a984c284：独立14/15→13/15，当时拒绝并恢复；
  现共用编码修复已接受，新部署/验收身份见上。
  高值单项0730db3b只测三题，父版2/3、候选1/3，不写成测过15题。
- 两个次正规候选43b0e688/e49bc64f：质量同父版，但性能门禁未通过。
  native次正规版8K TTFT三次范围同时差于前后父版，已拒绝并恢复。
  这些均不是当前运行时，不因低层参考通过而追认。
- 恢复HTTP三道变化题全文同父版：1K/44K正确、4K原错；这是有界
  恢复检查，不是新一轮完整矩阵。旧decode路径一致性实验亦未接受。

详见 [正确性恢复](CORRECTNESS_CONFIRMATION_2026-09-23.md)、
[尺度修正实验](E4M3_ROUNDING_2026-09-23.md)、
[高值单项实验](E4M3_HIGH_ONLY_2026-09-23.md)。

## 已有定位证据与尚缺什么

以下主要覆盖原4K单请求prefill。逐算子使用实际捕获输入，不能拼接成
“从原始token独立重算整模型”。高精度差异均保留；指定算术相同不
等于高精度相同，更不等于任务答案正确。部分末prefill/首decode
证据不能推广到后续全部decode、分块或其他上下文。

| 范围 | 已核对 | 未证明/限制 | 入口 |
|---|---|---|---|
| 模型入口 | 全请求embedding与四路展开；PLE查表SSD行及转换/缩放 | 非完整模型前向 | [查表](PLE_LOOKUP_REFERENCE_2026-09-23.md) |
| HC | 全4K残差、norm/mix、投影重放/FP64、门值及上下游交接 | 仍以实际主干为输入，非独立逐层传播 | [交接](HC_HANDOFF_REFERENCE_2026-09-23.md)、[投影](HC_PROJECTION_REFERENCE_2026-09-23.md) |
| Linear/GDN | 全输入投影、短卷积、q/k norm、零初态至末态全递推、NormGate/out_proj及残差消费者 | 指定精度参考含设备数学/cuBLAS；首decode与prefill精度合同不同 | [递推](GDN_PREFILL_REFERENCE_2026-09-23.md)、[输入投影](LINEAR_PROJECTION_REFERENCE_2026-09-23.md)、[输出](LINEAR_OUTPUT_REFERENCE_2026-09-23.md) |
| MoE路由/量化 | 全router/mapping/gather、输入及中间量化；E4M3错误确实到达实际计算 | FP64 router舍入后103行集合变化，未建任务因果；错误尺度未修正传播 | [路由](MOE_ROUTER_REFERENCE_2026-09-24.md)、[输入量化](MOE_INPUT_QUANT_REFERENCE_2026-09-24.md)、[中间量化](MOE_INTER_QUANT_REFERENCE_2026-09-24.md) |
| MoE专家/合并 | 全GU/down重放及FP64、路由合并、shared投影/SwiGLU及最终MoE合并 | 使用实际量化/路由输入；共享分支存在保留的高精度差异 | [GU](MOE_GU_REFERENCE_2026-09-24.md)、[down](MOE_DOWN_REFERENCE_2026-09-24.md)、[shared](MOE_SHARED_REFERENCE_2026-09-24.md) |
| PLE | 全prefill key/value投影、norm/gate/广播、卷积/历史及HC交接 | 逐算子条件参考，不是独立组合前向 | [投影](PLE_PROJECTION_REFERENCE_2026-09-23.md)、[卷积/门控](PLE_CONV_REFERENCE_2026-09-23.md) |
| QSA | 全query选择/KV/attention指定算术，主投影重放/FP64、norm/RoPE参考及实际消费者边界 | 完整indexer条件参考已补齐；未做独立整模型或高精度整链传播 | [完整QSA](QSA_FULL_REFERENCE_2026-09-24.md)、[旧局部范围](QSA_SELECTION_REFERENCE_2026-09-23.md) |
| 输出头 | 七次最终mixer、全词表logits、argmax、后续token交接 | 上游仍为实际主干，没有从原始输入独立生成 | [mixer](FINAL_MIXER_REFERENCE_2026-09-23.md)、[logits](OUTPUT_HEAD_REFERENCE_2026-09-23.md) |

参考依赖与接口见 [上游参考合同](UPSTREAM_REFERENCE_CONTRACT_2026-09-23.md)。
SGLang在本机的现有对照未形成稳定、可比的整模型oracle，不能据
“另一框架同错”证明本实现正确；旧ref_dump的精度路径也不能替代。

## 历史执行节点

槽位缺陷修复、真实HTTP生命周期及关键同分块交接验证已完成，
本轮有界状态阶段收束。长上下文稀疏路径、跨分块数值关系、视觉/MTP
等未覆盖项明确保留；请求取消已独立交付。真实代理与生成准入预算
的有界验证完成，默认loopback的后端监听合同已交付；控制预算
隔离与17轮有界耐久/后端重启恢复也已通过。双进程连接资源隔离
也已通过本地对照与reload/重启检查。后续商用
验收需真实业务负载、并发/队列预算与目标SLO，不能从当前有限
合成检查推导生产可用性；认证和实际部署网络仍未验收。
后续性能/复杂度优化以当前b5f38e48为测量基线，遵守五档E2E。


此前配对筛查（历史节点）：120 条新样本，原版 82/120、完整修复 80/120，3 改善、5 退化、
35 两版都错。5 道退化题双版本各复查一次，全部逐字复现；302 条
原始 HTTP 审计完成、终态封存。旧检索 11/11→11/11，旧推理 14/15→13/15。
新集总分、3 类任务与 2 档长度下降，按冻结门槛不进入性能验收。
该轮默认二进制未变、未做性能。这是有限合成筛查，不是总体质量证明。
详见[质量评估结果](QUALITY_REVIEW_RESULTS_2026-09-27.md)及
[冻结计划](QUALITY_REVIEW_2026-09-27.md)。

用户明确Runner对数值执行负责。历史筛查拒绝结论保留，
不再作为数值修复否决依据；完整修复的有限域接入与26题HTTP
核对已完成，五档性能采集通过。本次缺陷收口，不追加语义样本、
舍入组合或逐层传播。后续工程以已修复基线出发，仍按五档E2E。
全局异常输入fail-fast、状态/缓存/分块一致性及商用业务质量
属于后续独立验收，不能据本次局部通过冒称已经覆盖。

## 历史性能参考（b5f38e48）

当前五档参考为2026-09-27监听地址构建b5f38e48，记录在
[performance_reference.json](../tools/evalscope/fixtures/performance_reference.json)。
五档各三次、输出256、MTP关闭、单流；范围与比较见监听地址报告。
旧432fc489归档为performance_reference_pre_listen_address_20260927.json，
更早参考保留。新参考是实测身份更新，不是严格无回退证明。

| 输入token | TTFT均值秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.797 | 18.577 |
| 4096 | 2.627 | 17.885 |
| 8192 | 5.170 | 18.124 |
| 45056 | 30.451 | 17.860 |
| 204800 | 159.311 | 17.109 |

已接受工程基础包括GRFrame、normed复用/布局单源化、MoE暂存别名、
资源合同、GRWrite→GRRead融合、成对gate/mix、线性scratch所有权、
serve读回/提交边界、长文本分块调度及按消费者缩减输出头/logits。
这些不等于完整数据流引擎；各阶段验证与被拒绝实验见
[历史入口快照](HISTORY_STATUS_2026-09-24_CORRECTNESS.md)。
完整D/P/S、可执行计划和状态提交仍未实现，筹备见
[数据流项目状态](../dataflow-engine/STATUS.md)。

## 后续独立工作（不阻塞已冻结首版范围）

- MoE固定容量离线缓存回放已完成，结果与在线影子候选见上文；既定回放合同
  比较静态热点和简单动态缓存，
  遵守decode整组top-10、prefill块内专家并集和相同字节预算；先判断普通
  缓存收益，再决定在线影子统计或预测预取。受控采集已交付，真实offload、
  长期采样和专家驻留管理未做；下一会话范围与证据入口见
  [MoE路由分析与缓存回放](MOE_ROUTING_TRACE.md)。不为文档或纯离线工具
  重跑原HTTP矩阵，不自动开启运行时改造。

1. 随机文本的工具与服务端分词计数差异（目标1024，实际932/921/915）未解决。
2. 补真实语料、多轮及其他上下文质量；固定样本正确不代表全面长上下文召回。
3. 断连专项六轮恢复已通过；旧4K非确定性已修复。后续原4K独立求和
   错题在本次数值修复后正确，但1K/44K另两题变错；不能混同
   已修复的索引槽位/旧QSA数学问题。
4. 纯prefill时间尚未测量，TTFT不是纯prefill；细分析须满足现行门禁。
5. 约10.24GB/token、260GB/s对应25.4tok/s只是静态理想参照；没有
   当前完整时间账，不宣称decode已无优化空间。见 [静态预算](DATAFLOW_OPTIMIZATION.md)。

## 历史与证据

[本轮治理前完整快照](HISTORY_STATUS_2026-09-24_CORRECTNESS.md) /
[2026-09-21快照](HISTORY_STATUS_2026-09-21.md) /
[当天追加日志](log/2026-09-28.md)。专题报告与证据路径保留，历史
“运行中”“下一步”不再充当当前指令。全局索引仍见 [README.md](README.md)。
