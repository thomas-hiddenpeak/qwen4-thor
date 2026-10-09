# MTP 严格普通 greedy 基线：实现与验收范围

最终有限profile结论见[验收结果](MTP_STRICT_RESULT_2026-10-09.md)。
本文保留事前冻结规则与定向补件的时间顺序；其中“尚未执行”等
表述属于对应冻结时点，不替代当前STATUS与最终结果。

本阶段从研究提交 `2ca0986` 开始，属于持续Goal的B实现。基础
修复已交付PR #2，不能将新基线替代原快速T4分叉的独立解释。
默认MTP关闭；阶段只新增显式的可回退正确性路径，不改正式标签，
不宣称加速。严格路径预期较慢，其成本随后单独披露。

## 固定接口与实现边界

- `serve --mtp --mtp-verifier sequential` 为新路径；未指定verifier
  的 `--mtp` 保持既有 `t4` 实验行为。只支持S1、slot0、文本、greedy、
  k3。未启用MTP却显式指定verifier、非法枚举和generate入口均拒绝。
- `MtpSequentialResult` 含四格accepted_tokens、accepted_count、
  next_b/next_d0、terminal/next_seed_valid及draft/target/extend
  三类forward调用数。accepted只包含本步实际消费的非stop输入。
- `MtpSequentialVerify(main, mtp, seq, b, drafts[3], output_remaining,
  stop_tokens, result, stream)` 是生产复用helper；按普通scheduler
  的 `ModelDecodeBatchMulti(B=1)` 和 `model::ArgmaxBf16Rows` 逐行
  验证。前target_calls行logits/trunk留在已有d_ms_v*供extend及
  直接检查；不使用新的test-only draft override。
- `MtpSpeculativeStepSequentialTarget` 先产生真实draft（已有d0
  加两次forward），调用上述helper，再以实际target trunk追赶
  draft。非terminal仅一次extend；terminal不extend，seed无效。
- seq必须decode、无pending、history.size==position。core不改
  caller的position/history，只构造局部oldest-first PLE窗口；
  持model_mu_至完成并排空，成功后由请求线程发布消费前缀。
- 遇target预测stop即停止：next_b为stop、seed无效；返回已消费
  的非stop前缀。请求线程下一轮输出stop并计usage，绝不喂main。
  bonus已是stop时由请求层终止，core拒绝违反该前置条件的调用。
- 完整step要求输出剩余和context剩余均严格大于4；等于4也走
  普通scheduler B1尾部，因为普通路径不消费最后输出token。
  明确tail_reason=output_limit/context_limit，不把正常tail计为
  fallback。strict禁止降到另一算法ModelDecodeStepSeq。
- strict启动要求scheduler可用；主target未推进前的资源回退可
  进入同scheduler普通B1并记录实际fallback；main/extend中途
  失败则排空、结果失效、终止请求。每次submit及scheduler shutdown
  清空旧MTP结果，修正停机时可能复用上一步计数的问题。
- 显式sequential的max-prefill 1..3加载前拒绝（0仍取默认），
  防止workspace不足误标为正常context尾部。停机只修改仍pending
  的结果；MTP与普通尾部提交均在锁内检查stop，避免覆盖已完成
  结果或向已退出scheduler提交后永久等待。
- 所有scratch复用现有MTP池，不新增strict专属GPU分配。普通
  argmax自身既有lazy partial池约1,986,560B仍可能分配，位于固定
  margin内；不称整个调用链零分配或物理RAM已得到保证。

## 观测与工具

新路径为mtp_sequential_b1，实际verifier必须明确，记录target
T1调用（T4为0）、draft/extend调用及正常tail原因/计数。fast旧
路径与历史证据保持，缺少新字段的旧日志只可解释为legacy T4。
strict加载前拒绝旧cycle/verify-MoE计时配置，不伪造T4计数。
初始化计时不改变初始化算法，是否沿用按实际接口合同核对。

现有acceptance与同模式取消工具显式传递verifier并验证实际字段；
strict取消恢复增加tail原因/计数对照。旧decode-recovery仍保留
T4跨模式历史合同。B2a九请求输入与注入顺序保持，新的真实运行
要另外冻结最终二进制、输入和工具身份，尚未执行。
成功HTTP请求还绑定实际output：非terminal为target输入数加普通
tail输出数；terminal为target输入数加一个未消费stop。1K/256
恢复组固定output_limit，不能仅比较五条错误但相同的日志。

## 有界直接验证

本轮是数值/状态正确性实现，允许直接合同先于HTTP。先成组实施、
零警告构建；运行前锁定具名测试及产物目录。实现期间不运行模型。

- host检查覆盖options/模式解析和25格output/context剩余1..5
  的边界；stop、失败与发布须检查实际生产控制，不能用镜像脚本
  冒充核心已经验证。具体具名列表在首测前锁定。
- 真实48层模型：固定1024和8196两种前态，各4个自然step。
  对照是独立构造history和状态发布的普通scheduler B1轨迹，
  trunk_out=null；读取实际model.d_trunk作观察，核对capture副作用。
- 短前态受控a=0/1/2/3各一次：draft使用事前固定普通轨迹前缀，
  首次不匹配为(token+1)%vocab，不搜索prompt或seed。真实proposal
  轨迹另列，受控helper不替代自然完整step。
- 主recurrent/PLE、有效KV、idx_raw、已完成且可见idx_comp、RoPE、
  page table与host history分别比较。ModelSnapshotState只包含
  recurrent，禁止把其通过当作完整缓存恢复证据。
- 两种上下文各同前态/同输入的两套有限future-cache填充值，检验
  下一target logits/trunk/有效状态不依赖不可见future字节。
- stop、输出/context尾部、中途target/extend失败、取消后恢复均
  是Goal必验项；若本组尚缺真实运行或独立故障注入，应明确待验，
  不由host policy、自然成功或检查日志替代。

直接GPU组的尾部不采用手写server循环，避免镜像实现冒充生产
行为。移至下述实际HTTP admission-limits；直接主B1上限因此
收紧为92，保留96为原总上界，不用空余调用补样。

直接矩阵不超过约96次主B1与40次draft/extend，含两种前态的普通
prefill/capture对照。完整运行命令、fixture、状态有效域和空间
预算在执行前补冻；额外模型调用只能由具体失败定位说明。已通过
身份未变的证据复用，不为文档收尾重新运行。

首测前实现细化：sequential控制代码放在独立host TU，控制测试
直接链接生产代码并替换CUDA/forward依赖，覆盖接受0..3、8种stop
和两类部分失败；此测试不证明真实CUDA故障。独立GPU组中强制
a0..3只调用生产verify helper，不能称完整proposal/extend通过。
改为8个自然完整step逐个从独立普通trunk构造参考extend，实际
观察到哪些长度就只声明哪些长度的真实extend覆盖；不搜索补样。
上限为实际24次draft/extend、参考8次extend、初始化3块，共35次
MtpForward（主B1仍至多96次）。完整四形状接线由真实TU控制测试
补充，未覆盖真实CUDA形状必须在结果明确列出。

## 尚未由本阶段获得的结论

严格基线本身不能解释旧T1/T4分叉。快速路径的自然HC首差异、
真实状态writer/有效读域、未来缓存不可见性和离散选择仍需独立
合同与证据。后续统一HTTP代表性持续生成质量、失败/取消与五档
成本；原短答案11题不足以完成该质量出口。

## 后续HTTP质量组的事前边界

首次数值模型运行前已明确后续持续质量组：固定8题，四任务
（材料推理、Python代码、材料综述、逐字摘录）各中英1题；输入
长度1024两题、4096两题、8196两题、45056与204800各一题。
同一新binary先plain8、再sequential8，共16请求，greedy、S1、
8192分块/208896容量，每题最多512输出，实际输出至少128才有
持续生成覆盖。early EOS保存为覆盖不足，不换题、不补样。
正式题面/独立rubric在生成前保存并绑定SHA，工具不得由运行结果
选择题面或阈值。16请求合计最多8192输出，30分钟执行截止，
证据上限64MiB；这不是总体任务质量或生产语料代表性的声明。

配对数值接入比较完整未strip的UTF8文本、prompt hash、实际
input/output计数和finish；不要求SSE分块或request ID跨请求相同。
现有evalscope未保证保存usage-only原始SSE，因此total只作计数
之和的派生，不称观察到原始total_tokens或完整token IDs。
语义用材料独立事实/事前rubric另列，不能以普通输出当语义真值。
现有quality32与其11题保持，新增显式sustained-quality profile；
本组尚未准备tokenizer夹具、未运行请求，也未获得通过结论。

边界组另用显式admission-limits，旧limits六条保持：每模式14条，
1024输入下max_tokens=1/2/3/4/5/8各stream/nonstream，共12；
208891输入max8/stream与208892输入max8/nonstream各1。正式
容量使最后两条分别输出5/4，以length结束。strict前者须实际
mtp_sequential_b1，后者须plain_tail_b1，均记context_limit。
同binary plain14→strict14，逐对全文、usage计数、finish一致，
28条不追加有利样本；fixture生成后先绑定SHA再执行。

故障组仅使用独立EXCLUDE_FROM_ALL链接包装server，生产q4t没有
注入旗标或代码宏。五个1K/256请求固定为fresh control、第二次
实际target后逻辑Status失败、恢复、首次实际extend后逻辑Status
失败、恢复。包装器调用真实函数，观察原core的排空，不替其完成
同步；三成功结果同模式exact，两失败无普通fallback/正常完成。
核对slot回收、main Begin与draft Reset、stream排空及实际请求
序号。此项在真实异步forward提交后返回逻辑错误，由生产core排空已提交
工作；不声称注入返回前已确认全部device写入，不称硬件CUDA致命
故障或OOM恢复。原九请求取消恢复另验，彼此不替代。

起点源码快照：`.q4t-work/mtp-strict-20261009/starting-snapshot.json`，
258文件、2,611,656B，仅源码/工具，不复制模型payload。

## B3 首次执行清单（2026-10-09，执行前补冻）

统一构建目录为`build/mtp-strict-20261009`，只构建q4t、17组host
所用二进制、生产TU控制、独立完整状态、故障server及HC首分叉
六个显式目标。源码、工具、命令、日志及各二进制SHA由
`.q4t-work/mtp-strict-20261009/build-candidate.py`在构建前后记录；
零警告要求不变。所有首次失败原件保留，只有具体失败允许定向修复。

本地组由`run-host-01.py`事前保存具名合同：原13项受options/policy
改动影响而重验，加4项verifier与strict尾部；真实生产TU控制3项
覆盖接受0..3、8种stop与target/extend部分失败。五个Python模块
共108项，分别为acceptance mode、取消、response identity、持续
质量与故障恢复合同；8项实际进程检查新增入口/计时拒绝，使用
不存在的模型目录，每项15秒，不启动推理。host/control限60秒，
Python限120秒。未经身份变化的旧数值/HTTP证据不因此重复。

独立48层测试只运行`mtp_sequential_full_model_admission`一次，
总期限600秒；固定旧`mtp-batch-gather-20261008/direct-01`下
`prompt.txt`（SHA a72fade2611451e1f33ca088f0554474ec9d9032efccf04cc5581a2c25be2d98）
和`prompt-8192.txt`（SHA e4c57e9fca8d72d694b167a2f56f8527a51e5ab84830983e47ed434d0708e39a），
后者额外附加固定token 97/131/211/313，形成8196上下文。两种
上下文各4自然完整step；短前态受控verify四形状；各两种future
填充值。主B1不超过92、draft/init/extend不超过35，无补样。
有效域包含36层SSM/conv、全部PLE、12层KV逻辑已写前缀、idx_raw
前缀、idx_comp完成压缩前缀、全部page map及已写RoPE；先验证
page map为实际固定identity映射，再按连续页比较。host状态与
checkpoint元数据另验，不把未来未写cache当有效数据。快照约
短147.6MiB/长342.1MiB，draft约3.1/19.4MiB；host暂存约0.8GiB，
全trunk临时device约160MiB，保存raw上限96MiB。实际尺寸/调用数
以运行记录为准，超界失败，不缩减检查后称原合同通过。

后续HTTP总计最多110个生成请求：质量plain11/strict11、持续
质量plain8/strict8、尾部plain14/strict14、strict取消9、故障变体5、
五档plain15/strict15。全部外层清除Q4T_*及LD_PRELOAD；容量、
分块、采样、夹具及工具身份在首个模型测试前另存执行协议。
持续质量两个模式紧邻执行，共用30分钟期限及64MiB目录轮询
上限，含两次加载与退出；其余组不插入。指标仅作成本记录，
预计strict较慢，不引用快速T4性能为strict合格证据。

HC旧首分叉只做三次真实HyperConnectionMix（M1/M4A/M4B），
固定旧capture输入、不跑prefill或整模型；捕获normed/down_raw/
silu/up/mixed五阶段，15数组426240B。真实两投影各自应用事前
FP32归约gamma包络，三路first row共6个投影检查；须复现旧三路
mixed全文、M4 suffix变化的first-row独立性。非线性离线筛查另
冻独立算术工具，禁止由实测差异反推容差；CUDA intrinsic官方
误差表为观测界，不声称全输入数学保证。此组仅解释首处分叉的
有限算术来源，不将其等同整个快速T4计算与生成轨迹获准入。


## 实际失败后的长状态定向补件

首次combined直接组保留FAIL：原8192+4形状夹具的普通首预测为
stop248046（16.5，次名188为12.6875），capture/plain logits与
有效状态一致。旧批量Gather原件也为同一stop，却曾继续消费它；
旧局部形状对照不能据此宣称遵守真实停止合同。此处没有证据
证明追加四token单独造成EOS，未进行消融或搜索prompt。

缺失长decode域只补一次：从首次模型测试前已经绑定的持续
质量夹具中，按固定次序取首个8196题，即index4 zh-summary。
原始prompt SHA d9a2bc519332525d5c6f1e9d96b638faa76078a9c584c11e185ebefb47a20360，
不再附加token。新增独立long-only具名入口和EXCLUDE目标，旧入口
body保持，原二进制和FAIL不覆盖；共享调用计数器仅增加每入口
限额，旧默认96/40保持。构建与执行协议均在
`.q4t-work/mtp-strict-20261009/sequential-long-02/`首次执行前保存。

新诊断构建零警告；只执行该长入口一次，49.77秒PASS，实际主B1
28次、draft/init/extend18次、prefill4块、raw29,315,533B。
长4自然step、两种future不可见性、普通/capture前态、独立普通
target/trunk及extend有效状态exact且有限。两次直接组已实测
完整自然extend形状均为1和4，2/3仅由生产TU控制和受控verify
检查覆盖；不虚构缺少的完整GPU形状。短组未重跑，旧FAIL仍为FAIL。
此补件只改测试与CMake诊断目标，HTTP继续使用原a566faf5候选，
生产源码、工具和六个原二进制身份未变。

HC三调用首次通过，六套first-row投影共31680输出无包络越界；
独立非线性11个host边界测试首次通过，8640格全部PASS且允许
BF16区间均为单值。首差异确在相同normed后的down[81]：M1为
-12.3125，M4为-12.25；继而SiLU1项、up141项、mixed4项不同。
旧mixed全文三路复现，M4后续行变化不影响首行五阶段。
只读复核90/90通过，原复核初稿的两项来源路径误选保留并更正；
未重读权重重算全部点积，未证明内部具体归约树或哪路最接近
理想值。全输入保证/独立RMS/后续全层与快速T4完整生成仍不在
此有限解释中。intrinsic包络依据
[CUDA13.3数学函数表](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-programming-guide/05-appendices/mathematical-functions.html)
的观测界；Decimal.exp参考依据
[Python3.12定义](https://docs.python.org/3.12/library/decimal.html#decimal.Decimal.exp)。
