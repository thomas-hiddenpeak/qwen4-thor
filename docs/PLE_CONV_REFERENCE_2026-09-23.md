# PLE卷积历史与主干注入局部核对（2026-09-23）

## 结论与边界

生产代码和接受二进制未改。原4K质量题仍600440而非360284，
整模型正确性尚未确认。本轮只检查L1的PLE最后prefill token4095
与首个pooled decode token4096：卷积实际操作数、历史更新及最终
主干注入结果，不覆盖PLE hash/SSD行读取、FP8转换、投影、门控
和归一化来源，也不覆盖完整prefill或多流/MTP。

两个最终输出共20480个BF16，纯FP64参考（保留三次规定舍入）
和三种指定算术参考均逐字节相同。此前HC的L0输出→L1输入非恒等
边界，与本次PLE实际trunk输入/输出逐字节接通；prefill有9997项、
decode有9928项因PLE注入而改变，不能把它当作恒等层间交接。

## HTTP观测与实际参数

最小只读观测只拦截PLE单序列prefill卷积/状态更新，以及首个
pooled decode卷积/状态更新。按ple_layer源文件标识和实际核名
识别，避免与其他归一化/卷积同名函数混淆，不携带已有主注意力
大权重捕获。必要构建零警告后首项三侧tools/evalscope原4K HTTP
均4096输入、7输出、stop、600440，服务退出0、质量驱动1。四组
元数据完整且全部ok后才离线分析，不将观测不变称作质量通过。

实际C10240、K4、dilation3、state_len9；decode实际seq_id=0。
卷积权重逐字节匹配checkpoint的layers.1.ple.conv1d.weight，
形状[10240,1,4]。trunk_add实际非空并在原地写回前捕获；状态
更新实际input/state指针同前序卷积，输入快照保持不变。

prefill捕获最后10个归一化门控输入，末token四tap为行0/3/6/9；
decode捕获当前行，前三tap来自实际历史的列0/3/6，第四tap当前。
两阶段trunk输入匹配已有HC L0 write.output，最终输出匹配HC L1
read.trunk；先核验旧哈希清单摘要，再核验各实际文件字节。

## 历史与数值参考

prefill卷积前92160项历史全零；卷积后、更新前历史未改变。更新
后逐字节等于最后9行输入转置为[C,9]。这92160项完整传递给首
pooled decode；其更新结果逐字节等于旧历史移去首列并追加当前
输入。两个状态更新和跨阶段传递分别验证，不以最终答案相同替代。

FP64点积与SiLU参考保留生产融合语义：先将SiLU舍入BF16，再
与gated相加舍入BF16，再与trunk相加舍入BF16。最终20480项全同。

指定算术由CPU顺序四次FP32 fma重建卷积，预声明三种非线性：
CPU exp再FP32除法、独立设备__expf加CPU除法、独立设备完整SiLU。
后续三次BF16舍入及两次加法均由CPU完成；三种最终差异数0/0/0。
全部参考中间输出保留，但融合生产核没有暴露实际SiLU/PLE中间值，
因此最终相同不能证明内部每个值相同，也不能从零差异辨认数学库。

参考工具首次构建有misleading-indentation警告，分析入口在数值
计算前被零警告断言拦下；原源码、警告和拦截日志保留。仅给参考
代码补括号后重新零警告构建，继续同一已通过观测检查的HTTP快照，
没有改生产/观测器或重新选择HTTP结果。最终参考与审计退出0。

## 证据与后续

证据目录`.q4t-work/e2e/ple-conv-reference-20260923/`含三侧HTTP、
fp64-reference.json、conv-reference.json、hc-binding.json、
artifact-binding.json、历史快照及全部变体输出。没有新的性能结论。

下一步向前连接PLE门控和分组归一化的实际输入/权重，然后投影、
FP8转换、hash/SSD查表来源。全prefill GDN状态独立生成和最终头部
等仍未确认，不因这两个token的注入结果相同而省略。


## 后续：三次归一化与门控来源

新观测按ple_layer源文件标识捕获实际GroupedRmsNorm、PleGate、
GatedValue。每个阶段依次key/query/conv三次norm，各实际指针
绑定gate两输入、gate→gated乘法、gated→conv norm，以及conv
norm/gated→上一轮卷积实际参数。prefill保留末10行4086–4095，
decode保留首行4096，因此前一轮卷积所需的所有10行输入均被覆盖。

观测零警告构建后首项三侧原4K HTTP仍600440、4096输入/7输出、
stop，服务退出0、质量驱动1；10组新元数据完整，此前20份卷积
记录逐字节不变，才进行离线分析。不是质量验收通过。

三组norm权重各10240项，实际字节均同checkpoint中的
layers.1.ple.norm_key/norm_query/norm_conv.weight [10240]，
epsilon为FP32的1e-6。query原始输入最后行同实际trunk；norm
key/query输出同gate输入；gate输出同gated输入；gated输出同
conv norm输入；conv norm全部输出同卷积所读末10/当前1行。

CPU按每lane八项平方顺序fma、每lane十组累加、warp XOR归约，
FP32均方加epsilon，再用x*(rs*(1+w))及BF16舍入。纯CPU rsqrt
版本337920输出有1项不同：prefill conv norm展开索引5390。
仅替换为独立设备rsqrt后全部位同；纯FP64参考另有4项不同。
三类结果与完整索引保留，不改变容忍阈值或重采样。

Gate实际scale逐位同host的1.f/sqrt(float(2560))；CPU按2560项
顺序fma生成点积后乘该scale，再sqrt(abs(raw)+1e-6)带原符号，
最后sigmoid/BF16。CPU数学函数与独立设备sqrt/__expf版本均44
个门值位同，使用实际scale的FP64参考也同。GatedValue将门值
广播到每分支，FP64乘法再FP32→BF16的112640结果全部位同。
这些局部输出均直接捕获，区别于融合卷积仅能比较最终结果。

全部构建零警告、参考和审计退出0；生产未改，原质量失败仍在。
证据目录`.q4t-work/e2e/ple-gate-norm-20260923/`包含三侧HTTP、
fp64-reference.json、gate-norm-reference.json、previous-binding.json、
artifact-binding.json以及CPU/设备数学函数全部参考输出。

这补齐给定key/value投影输出下的PLE门控、归一化到卷积链路，
不是key/value投影或SSD embedding来源证明。下一步检查这两组
投影的实际权重和输入，再追溯FP8转换、hash与SSD行读取。


## 2026-09-24后续：完整4K卷积与历史边界

此前参考仅覆盖prefill末行及首decode。新只读观测器扩展到完整
4096个位置；零警告构建后第一项为tools/evalscope三侧HTTP。
关闭/开启/关闭观测均输出原错误答案600440、4096输入/7输出、
stop、服务退出0、质量驱动1。原错答保持不代表质量验收通过。
四份元数据均通过，所有采集标记出现在服务监听之后。

实际卷积权重81920字节直接匹配checkpoint
`model.language_model.layers.1.ple.conv1d.weight`，形状10240×1×4。
完整prefill输入/gated/trunk/output保存；41943040个主干输入
逐字节等于HC层0 write.output，41943040个最终输出逐字节等于
HC层1 read.trunk。此前16份末尾/小张量证据摘要重读后全部一致。

初始92160个历史值全零，prefill更新严格等于本块末九行转置；
首decode历史等于该更新，随后左移一位并追加新行，更新全部一致。
实际卷积输入gated_n及gated来源尚未独立重算，本阶段未执行
卷积算术参考，不能把历史与交接一致报告成完整PLE正确。

证据：`.q4t-work/e2e/ple-full-conv-20260924/`；模板：
`tools/verify/ple_full_conv/`。含完整HTTP、checkpoint片段、原证据
与HC绑定、全部采集及manifest。采集后可用22285783040字节。
生产未改，下一步按分块有界临时空间重建全位置卷积与三次BF16
舍入，保留全部参考差异，再补上游门控、归一化和投影。
