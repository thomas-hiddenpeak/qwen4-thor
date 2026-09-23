# Hyperconnection残差及层间交接（2026-09-23）

## 结论

原4K请求48层，取prefill末token和首个pooled decode。attention
融合写回/分组归一化与MLP独立写回分别核对，共192次、1966080个
BF16残差输出，同FP64参考舍入，也同CPU独立fma后BF16舍入。
实际812条边界逐字节一致；与上一轮1872份BF16生产快照相同。
生产未改，三侧HTTP仍600440（应360284），原质量失败未修复。

这里确认的是已捕获注入门、子层输出及残差条件下的写回与交接。
不是独立生成注入门或混合权重，也不是完整hyperconnection正确。

## 观测归属的两次失败

首次只按GroupedRmsNormKernel短名识别，PLE/MTP/HC同名核造成
句柄覆盖。实际406份而非480份，完整性门禁失败；before/observed
HTTP保持原错、服务0，after未运行，也未做低层数值分析。
接受二进制的设备stub明确包含不同源文件标识，修订限定
hyperconnection_cu。初版失败数据与诊断保留在hc-handoff目录。

v2三侧HTTP及480份计数通过，但身份检查发现decode read错位。
prefill输出头只取最后一行，另执行一次T1最终混合，旧逻辑误将它
当成第0层decode读。错误归属的数据逐字节等于第47层prefill
输出，后两份分别等于实际decode第0/1层残差。源码model.cu的
LogitsRows::kLastRow→HeadForward、model_head.cu的mixer调用与
此证据吻合。v2身份分析退出1，CPU两类combine重算退出0；不能用
该轮计数通过或combine一致宣称跨层身份通过，原始数据保留。

v3捕获prefill第0层实际HC norm权重指针，以其首次T1调用确定
decode起点，排除最终输出头混合。再次零警告构建后首项三侧HTTP，
三次原错600440、4096输入/7输出、stop，三服务0、质量驱动1。
480份完整之后分析身份，812条全部通过。两次重跑用于修复观测，
不属于重采样寻找质量通过；没有修改生产实现或舍弃失败证据。

## 覆盖与计算

每阶段48份read、48份attention融合写回、48份MLP写回，及96份
attention/MLP混合，共480份。核对hc=4、hs=2560，取实际输入、
输出和注入门；归一化/混合输出本轮作为现场边界值，不独立重算。

812条身份包括：

- read的trunk同attention写回所保留残差；归一化输出同混合输入。
- attention写回结果同MLP所保留残差；融合归一化同MLP混合输入。
- 全48层MLP混合输出/写回block输入，分别同此前MoE输入/输出。
- 36个linear层attention混合输出/写回block输入，分别同实际输入
  投影输入/输出投影结果。12个full-attention内部边界另待覆盖。
- 每阶段46条无PLE干预的相邻层写回→下一层trunk逐字节相同。

第0层输出到第1层read之间有PLE添加，两阶段均不同且单列保留。
它不是恒等交接，不通过放宽数值误差处理；PLE运算仍未独立证明。
第0层trunk来源和最终输出头也不在本轮证明范围。

残差参考为R[b,c]+block[c]*inject_gate[b]。FP64参考用实际BF16
三个操作数，先转FP32再最近偶数舍入到BF16。另用CPU libm fma
显式单次FP32融合乘加、BF16最近偶数舍入，编译关闭隐式收缩；
192次1966080输出全部位同，原始结果文件逐字节复核。注入门的
生成、分组归一化、低秩投影及混合sigmoid仍待独立核对。

## 证据与下一步

证据在`.q4t-work/e2e/hc-handoff-v3-20260923/`：
raw-http-audit.json、handoff-reference.json、cpu-reference.json、
previous-binding.json、artifact-binding.json和observed-captures/。
无版本与v2目录保留observer-diagnosis.json、失败数据及终止记录。
生产二进制仍2392f6b1，MTP关闭；诊断时间不作性能验收。

下一步建立HC norm、mix_down/up、inject投影与门控的独立参考，
优先绑定实际权重和输入；随后继续完整prefill GDN状态、QSA、PLE
与输出头。当前局部一致不能作为原4K错误与实现无关的证据。

## 后续：混合sigmoid与四分支加权参考

复用已完成三侧HTTP及身份验证的v3冻结快照，未改观测或生产实现，
未另跑模型。读取并校验上一轮artifact-binding全部条目；本轮192组
up/normed/output共576份输入文件分别记录SHA，分析后再次校验不变。

48层×prefill末token/首decode×attention/MLP，共192次、491520
个BF16混合输出。数学参考为四分支sigmoid(up)*normed之和除以4，
使用FP64后经FP32→BF16最近偶数舍入，有8项不同；索引及参考值保留。

独立CPU参考按分支0/1/2/3顺序执行显式FP32 fma，除4再BF16舍入，
关闭隐式浮点收缩。CPU sigmoid使用double exp舍入FP32、FP32加法与
除法，有6项不同：L30 decode attention/1940，L35 decode MLP/1360，
L36 prefill MLP/1170，L37 decode attention/1152，L42 decode MLP/1216，
L45 decode MLP/2409。没有改变容忍阈值或删除这些差异。

设备只从实际up值重建1/(1+__expf(-up))，不调用生产MixGatePair。
将其结果交给同一个CPU四分支fma累加器后，491520个BF16全部位同。
所以本次6项纯CPU差异可由sigmoid算术来源解释；FP648项包含不同
精度、累加和非线性共同影响，不逐项仅归因为exp。不是完全独立于
CUDA数学近似的证明，也不是up/normed来源正确的证明。

两份工具零警告构建、运行退出0；控制器逐文件复核差异计数并保存
全部输出。证据目录`.q4t-work/e2e/hc-mix-reference-20260923/`中的
input-binding.json、fp64-reference.json、mix-reference.json及
artifact-binding.json；原HTTP质量失败仍明确保留。下一步绑定实际
HC norm权重，核对归一化与mix_down/up、inject投影及注入门生成。

## 后续：HC分组归一化权重与数值参考

新观测只补实际HC norm权重及epsilon，保留v3来源识别和decode
起点规则。零警告构建后首项执行原4K无/有/无观测HTTP，三次仍
600440、4096输入/7输出、stop，服务0、质量驱动1。480份旧边界
及192份归一化权重/epsilon完整后才分析；3504份旧BF16快照位同。

48层attention/MLP两组、prefill末行/首decode两阶段，共192次。
实际权重逐字节匹配对应attn_hyper_connection或mlp_hyper_connection
的hc_norm.weight，均10240个BF16；实际epsilon按FP32位值检查为
1e-6。attention使用现场trunk，MLP使用融合写回后实际BF16 residual，
每个4×2560分支分别RMS归一化再乘(1+weight)。没有跳过写回的
BF16舍入，也没有把归一化权重当作直接scale。

1966080个BF16输出对FP64独立参考有10项差异，全部索引及数值保存。
另用CPU重建每lane八个平方的指定fma链、10组局部累加、warp XOR
16/8/4/2/1归约，FP32均方+epsilon与乘法分组。CPU double倒平方根
舍入FP32时剩2项：L30 decode attention的9338、L40 prefill
attention的5018。仅用设备rsqrtf重建4个分支的倒平方根，其他
归约和乘法仍由CPU完成，全部1966080个输出逐位一致。

该结果解释本样本指定运算下的局部差异，仍非完全独立于CUDA数学
近似的证明；FP64的10项差异不全部仅归因为rsqrt。初次CPU工具
构建有缩进警告，保留日志并修正为零警告后才执行；数值工具均退出0。
审计工具初次把order目录当作二进制文件计算SHA失败，修正为实际
order/order路径后完成文件哈希与192份原始输出逐元素复核；初版
审计脚本留存，不将该打包错误当作数值失败或掩盖数值差异。

证据目录`.q4t-work/e2e/hc-norm-reference-20260923/`，主要记录
norm-reference.json、order-cpu-audited.json、order-device-audited.json、
previous-binding.json和artifact-binding.json。接受二进制及生产
源码未改，原4K错答仍在。接下来核对mix_down/up与inject投影和
门控生成，将实际normed输入继续接到低秩计算；完整模型尚未验收。


## 后续：原4K完整残差与边界

新增最小观测器仅截获hyperconnection_cu四类kernel的T=4096调用，
48层read/fused/write及96次mix，共240条元数据；排除最终T=1 mixer。
零警告构建后首项tools/evalscope原4K无/有/无观测HTTP，三次仍
600440、4096输入/7输出、stop，服务0、质量驱动1。只通过观测
不改变本请求结果的门禁，任务正确性未通过，不使用观测耗时验收性能。

全量捕获预算48323494272字节，CPU残差参考8053063680字节，另
预留21474836480字节；开始时可用107436716032字节。实际原始
数据与参考输出均保留，未删除旧失败证据。原plan中的capture_limit=36
是复制遗留字段，实际代码/240条元数据覆盖48层；原记录不改，附
scope-clarification.json澄清并修正永久模板。

CPU逐元素从实际block/gate/residual重建fma后BF16舍入，96次写回
共4026531840个输出逐位一致。另以double乘加后转FP32再舍入BF16
也无差异；这不宣称不经过FP32的直接FP64→BF16舍入已经验证。
完整CPU输出及差异索引文件保留。实际注入门值和block输出仍是
观测输入，本结果不独立证明产生它们的全部计算。

311条完整字节一致关系包括192条层内交接、46条直接层间交接、
1条embedding四分支入口、72条linear输入/输出边界。旧证据先按
对应manifest核对再绑定；912份末行或权重与旧HC记录一致。
96组归一化权重匹配checkpoint，epsilon均为FP32的1e-6。
L0写回到L1读入之间有PLE，不能要求直接相同：40249033个元素
变化，完整两端及每行变化计数保留，本阶段未独立重算这段全行PLE。

证据目录`.q4t-work/e2e/hc-full-boundaries-20260923/`；summary.json、
residual-reference.json、boundary-binding.json、checkpoint-binding.json、
previous-binding.json及artifact-binding.json可审计。工具模板位于
`tools/verify/hc_full_boundaries/`。生产源码/接受二进制未改。
下一步复用冻结快照核对全位置HC归一化与混合，再补全投影来源；
全位置MoE/QSA/PLE及其他请求仍不能由这些局部一致性替代验收。


## 2026-09-24后续：完整4K HC归一化

复用上一节已完成三侧HTTP观测一致性检查的冻结快照；先核对
source manifest和384份实际输入/权重/epsilon/输出，再执行参考。
没有新生产候选或新观测器，没有重跑HTTP，也不能标成质量验收通过。
参考所需输出/均方/设备倒数约24.56GB，开跑可用51.06GB，另预留
20GiB；全部变体及所有差异索引保留，未删旧证据。两工具零警告构建。

48层read/fused两类，共96次、4026531840个BF16输出。融合路径
使用已经核对过的实际BF16写回结果，保留残差舍入边界；权重按
checkpoint逐字节再核对，缩放为(1+weight)，非直接weight。
CPU重建每lane八个平方的fma链、10组局部累加、32个lane各自的
XOR16/8/4/2/1归约、FP32均方加epsilon，以及x*(rs*(1+w))乘法分组。
本样本1572864组最终均方的32个lane结果恰好全同，但参考没有
预先用lane0代替其他lane。

纯CPU倒平方根参考13832项差异；仅改为设备rsqrtf，其余计算仍由
CPU执行时，全部输出逐位一致。独立double平方和、倒平方根和乘法
再转FP32/BF16的公式参考29533项差异，全部保留；不宣称CUDA
数学近似被完全独立证明，也不把FP64差异一概归因于rsqrt。
全行末位置的CPU/device差异索引与旧局部参考192条检查一致。

证据`.q4t-work/e2e/hc-full-norm-20260924/`包含384份input-binding、
全部CPU/device/FP64 BF16输出、均方及设备倒数、完整差异索引、
audited-records、previous-last-row-binding和artifact-binding。
工具模板`tools/verify/hc_full_norm/`。生产源码/接受二进制未改，
原4K错误未修复。下一步全位置HC混合算术；磁盘余量约25GiB，
下一阶段须先预算存储，不能直接复制更多大规模中间量。


## 2026-09-24后续：完整4K HC混合

继续使用hc-full-boundaries冻结快照，不改生产/观测器、不新增HTTP。
288份up/normed/output先逐份核对来源manifest；96份normed与上一
阶段完整CPU算术+设备rsqrt参考输出按全文件SHA绑定。两个工具均
零警告构建后才运行离线参考。

48层attention/MLP两类，共1006632960个混合输出。设备仅计算
65536种BF16编码对应的sigmoid表，不调用生产混合kernel；实际
up和normed逐项断言有限值，CPU按四分支顺序fma、除4、BF16舍入。
该参考全部逐位一致。CPU将double exp舍入FP32后做sigmoid及
同序fma时9962项差异；double sigmoid、乘加及除4后经FP32/BF16
舍入的公式参考25801项差异。全部保存，不把数值复现当作独立
硬件精度证明。末行CPU/device的192条差异索引检查与旧参考相同。

为控制磁盘用量，每组先生成三份完整参考，保存全部差异索引和值，
以已绑定原始输出为基底还原，逐元素及完整SHA256均相同后，才移除
该组新生成的临时完整副本。最终审计再次重建全部输出验证SHA。
这是一种无损保存，并未省略相同位置或差异值的可恢复性，也未删除
旧证据。每组开始要求20GiB预留加1GiB工作余量；新阶段结果约数MB。

证据`.q4t-work/e2e/hc-full-mix-20260924/`：reference.json记录每组
完整输出SHA、差异数及delta包SHA；reference/*.delta.npz为全部
差异位置/值。input-binding、norm-boundary-binding、
previous-last-row-binding、summary及artifact-binding记录完整链。
工具模板`tools/verify/hc_full_mix/`。生产未改、原4K错误未修复。
完整up仍是实际投影输出，下一步补HC低秩down/up及inject全行
算术和门值来源；不能将残差/norm/mix一致扩大为整个HC独立前向。
