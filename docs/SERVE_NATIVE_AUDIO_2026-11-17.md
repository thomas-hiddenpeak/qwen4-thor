# 原生音频后端与标准接口实施

A01/A02/A07/A09/A10/A12的音频缺口继续属于总任务。当前主模型
checkpoint只有文本/视觉，现有音频字段占位不能执行转录或发声。
新增真实语音模型和原生执行路径；不能用空结果、文本回显、统一拒绝
或强制外部推理服务替代。rev266保持已验默认，候选通过后再发行。

## 子目标与依赖

1. **ASR01资产及数学合同**：固定Qwen3-ASR-0.6B的官方revision、
   配置、tokenizer、权重形状与dtype；原生严格解析，拒绝语义漂移。
   保持原主模型目录和reference只读。模型资产按部署显式启用，
   不随核心包强制下载，失效只使相应音频能力不可用。
2. **ASR02原生计算**：有界音频解码/重采样、128维log-mel、音频
   encoder、Qwen3 decoder和实际生成。复用现有CUDA/线性层/隔离
   worker能力；可参考qwen35-thor的MIT实现，移植时保留许可并在
   README注明。不得直接带入其1.7B双分片假设或固定中文提示。
3. **ASR03服务合同**：原生`/v1/audio/transcriptions`的multipart、
   language/prompt、实际usage、JSON/text/SSE、错误/取消/资源预算；
   原生入口、模型登记、Python/JS SDK和服务包一致接入。标准的其余
   参数与输出格式逐项实现，时间戳需实际对齐，不能均分伪造。
4. **ASR04业务与恢复组合**：中/英文真实音频、流式、客户端与
   故障隔离，主/轻profile、原生TLS和可回退交付。复用身份未变的
   文本/视觉证据，不重跑完整模型正确性或五档性能。
5. **后续音频能力**：真实对齐/说话人、Chat音频输入与非空历史、
   实际语音输出、Responses/Batch组合及Realtime。转录交付仅关闭
   对应子项，不关闭整个A10；图像/视频生成、其余媒体及其他master
   任务保持待实现。

## 固定依据与依赖

- [OpenAI转录参考](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
  2026-11-17抓取；本地已有Python/JS固定SDK与新增网页字段分别记录。
  SDK缺字段不代表服务可以丢弃。Docs工具的OpenAPI片段有未展开ref，
  不能据此声称请求schema审计完整；结合正式页面和SDK原定义核对。
- [Qwen3-ASR官方实现](https://github.com/QwenLM/Qwen3-ASR)作为计算
  和模板依据；代码来源需固定commit。官方字/词时间戳使用独立
  ForcedAligner，当前0.6B转录权重不冒充该后端。
- [Qwen3-ASR-0.6B固定资产](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/tree/5eb144179a02acc5e5ba31e748d22b0cf3e303b0)，
  Apache-2.0；权重1,876,091,704字节，SHA256
  `79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea`。
  官方提供vocab/merges/tokenizer_config，没有tokenizer.json，须按
  真实BPE/特殊token/normalizer合同接入，不借用主模型tokenizer。
- 参考qwen35-thor commit `57e29777c2aff8a97f42df6e3d9487b1327f014f`；
  本轮先审查，不把参考服务或其Python依赖加入默认发行。
- 不新增必需nginx、vLLM、Transformers或FFmpeg可执行程序。解码
  优先复用项目的隔离codec；必要系统库仅在启用的后端内使用，
  精确依赖及失效范围随实现登记。

## 当前冻结边界

ASR01先获取固定资产并实现配置/权重合同；下载只写
`.q4t-work/serve-audio-native-20261117/`，总资产不超过2,000,000,000
字节，预留至少8GiB磁盘空间，1800秒上限；每个文件核对固定来源
的长度与Git blob或LFS SHA256，保留失败和未完成文件。不开GPU。

首组CPU最多8项、180秒：官方配置通过及架构/token/数值漂移拒绝，
encoder分块长度的边界，完整权重名字/shape/dtype/区间合同以及
实际checkpoint只读核对。必要构建单列，不运行模型。之后继续
tokenizer、预处理和真实后端；各组实施前固定直接用例与GPU短验
预算，不在本阶段结束时停止总任务。

ASR01完成不等于音频接口可用，也不触发默认发布。PR43在既有
最终head独立完成检查与实际合main；本组使用同一工作分支的后续
提交，不漂移已验包或用户独立长稳快照。

配置/布局及完整checkpoint共8项CPU已通过。tokenizer固定官方
Qwen2Tokenizer的实际输出后，新增
原生vocab/merges/tokenizer_config入口，保持主模型和embedding
profile的原有合同。预算8项CPU/300秒：ASR官方中英文/Unicode/
特殊token及空文本，无隐含EOS，加载污染与限额/取消，原两profile
直接回归；参考tokenizer只在离线CPU验证使用，不成为运行依赖。

tokenizer的8项已通过（2.262秒），16组官方编码/解码一致，原两
profile直接回归通过。完整资产1,880,618,159字节、1532.417秒，
Git/LFS校验全部通过，仍有12.657GB空余。参考环境为Transformers
5.16.1/tokenizers0.23.2，其来源摘要保留；首次驱动把Torch导入当作
CUDA初始化而拒绝，原失败保留。修正后显式无可见GPU并核对CUDA
未初始化，参考仅用于CPU编码，没有加载模型或运行CUDA。

## ASR02特征计算冻结

先实现16kHz单声道PCM至128维log-mel的原生数学路径：400点周期
Hann、中心reflect填充、160采样hop、Slaney三角滤波与面积归一化，
功率谱/log10、8档动态范围裁剪和最终缩放。序列长度按实际STFT
帧计算，不假造尾帧或截断输入；解码/重采样和encoder另接后续路径。
沿用现有C++标准库，先不增加FFT运行依赖，不作性能优化主张。

CPU上限8项/180秒：6个固定合成PCM的完整特征矩阵与官方NumPy
参考差分（零值、冲激、单频/多频、噪声及非整hop），最大绝对误差
1e-5；另2项覆盖长度/输出预算/非有限输入与取消/失败原输出保持。
参考只在CPU生成固定夹具；运行时不依赖Python/NumPy，0模型动作。
通过后继续实际音频解码、encoder/decoder和HTTP，不能据此宣称ASR
服务已完成。所有其他master任务保持原范围。

特征8项已通过（0.289秒），六组矩阵最大绝对误差均为0；累计
[24项基础CPU证据](evidence/serve-asr-foundation-cpu-20261117/README.md)
2.571秒、0模型动作。固定资产、原失败和三份已验CPU程序保留。
下一步真实音频解码及原生encoder/decoder，A10保持进行中，默认
rev266与用户独立day身份不变。

## ASR02 encoder/decoder计算合同与冻结验证

实际实现采用固定0.6B权重：3层stride2卷积/GELU、18层音频encoder、
独立输入表/输出head和28层Qwen3 decoder。复用本项目已经验收的
普通Qwen3 RMSNorm/RoPE/因果注意力计算；ASR不套用主模型中心化
RMSNorm，也不把输入embedding和lm_head按配置自动别名。

每100帧mel单独卷积，尾块补齐到本条音频的最长块长度后经过全部
卷积，再去掉无效位置；不能在第二/三层提前丢掉带bias的填充区。
卷积后位置编码每块从0开始，常规13位置/块、8块/注意力窗，即104
位置。官方固定源码的FA2使用cu_seqlens，但forward没有向eager/SDPA
传入其已有的分段mask；不能把无分段参考当正确的长音频合同。
差分参考将明确加同一cu_seqlens块对角mask；这是记录在案的参考
适配，不能声称与未修改的eager路径等价。浮点路径先采用可追踪的
BF16算子舍入及FP32归约；不声称与FA2逐位一致。

CPU最多8项/180秒：真实tensor绑定布局、卷积/注意力窗与尾块、
工作空间/整数边界及原输出保持。构建单列，默认不变。计算完成后
再固定数值/真实音频GPU短验驱动及请求数；未到该步骤不加载GPU。
首版计算按请求显式资源预算，后续服务层继续实现格式解码、转录
输出合同、实际对齐及全部音频任务；不能把计算组件当音频API交付。

## 首组真实计算短验冻结（2026-11-17）

8项布局CPU于0.007秒通过；原生CUDA与隔离驱动已构建，默认引擎
未动。先进行组件数值/真实转录验证，再接HTTP；这是功能开发，不
做性能接受结论。参考固定官方源码7c6daf77及Transformers4.57.6，
CPU PyTorch2.14.0+cpu；不会把5.16导入失败当模型失败。依赖仅在
工作目录内用于对照，不进入核心发行。

同一gpu_exclusive严格租约：最多1次原生GPU加载、1次CPU参考模型
加载，2段官方中/英文音频（4.204/15.051秒），原生最多256次decode、
参考最多264次decoder前向，总1200秒、各子进程600秒、0HTTP。
英文覆盖跨104位置窗口。参考音频准备为stdlib PCM WAV解码与显式
soxr HQ重采样；原始wav/准备PCM/mel摘要固定，尚未验收原生codec。
不干预其他服务，不自行恢复默认，不运行主模型完整正确性/五档性能。

判定在运行前冻结：实际PCM的原生mel与参考完整矩阵误差<=1e-5；
encoder输入相对L2<=0.01、余弦>=0.99995；encoder最终输出及相同
历史下前4步logits相对L2<=0.04、余弦>=0.9995；4步argmax一致，
两段真实自由生成文本一致且均正常EOS。各项同时满足才接受组件，
无通过则保留失败并定位，不能通过改阈值追认。该BF16跨实现差分
不宣称逐位等价、通用识别准确率或音频服务交付；API、codec、取消/
资源与恢复组合继续实施。具体驱动/阈值在tools/native_asr/acceptance.py。

首轮原生1次加载/2段生成已完成（3.618秒、60次decode）；严格租约
无竞争，默认五项身份保持。但CPU参考在加载后、前向前触发“输出
head不可共享指针”断言，整组如实FAILED。追加只读核对确认两份
独立存储的311164928字节tensor完全相同（SHA256 ab067cf4…），
因此官方按配置共享内存有效。原生继续读取两份真实权重，不作优化。

针对参考夹具修复只补一次CPU参考加载/最多264前向、600秒、0新增
GPU加载；经新的严格租约运行，原GPU二进制、输入与输出摘要逐项
核对复用，不重跑已完成原生生成，不覆盖原失败。阈值全部保持。
累计模型加载为原生GPU1次、CPU参考2次（第一次未前向）；API和
完整音频业务仍待后续交付。

补参考加载后两段生成文字完全一致，encoder输入/输出4组通过，
8组logits中5组超过原冻结阈值；保留FAILED，不以转录成功覆盖。
下一步只针对失败链做同操作数隔离：复用两端完整encoder向量、
相同的8个decoder前缀，CPU1次加载/16前向/180秒，0新增GPU。
先逐位复现原CPU8组logits，再使用原GPU音频向量交给同一CPU
解码器，以区分encoder误差传播和decoder计算差异；这是定位，
不能据此自动追认原阈值通过。原默认和全部失败证据保持。

CPU隔离16前向7.209秒完成，8个原CPU结果逐位复现。同音频输入下
仍有decoder差异，中文第一步相对L2为0.124，不能归因于encoder
单一路径。下一组只抓中文首步：原生GPU1次加载、1段encoder、
1步decoder，180秒上限；诊断编译开关输出28层实际操作数和最后
norm。要求encoder与首步logits逐位复现原捕获，否则不能据此定位。
原生生产算术不改，已有完整生成和原CPU结果复用。随后CPU逐层
同操作数核对，以找到局部算子错误或量化/归约误差来源；不改原
冻结阈值，不将定位通过冒充组件或服务交付。

诊断捕获已完成：中文encoder输入/输出和首步logits均逐位复现，
1849份操作数共125227008字节。接着只分析已捕获同操作数：CPU
300秒、0模型构造/0GPU，核对28层实际归约、所有投影/QK/PV的
FP64点积与FP32累加误差界、BF16舍入区间、整头共用rsqrt的一致性、
RoPE/重排/残差/softmax/SwiGLU及真实lm_head。保留负对照；rsqrt/
exp的有限ULP检查只说明捕获实例相容，不声称全输入普适保证。
无论结果如何，原端到端FAILED和原阈值不改；该分析是独立数值
证据，不自动替代服务合同或发布验收。

前27层共1836项实际操作数检查无违例；原检查器在末层遇到有限
负输入的FP32 exp溢出而中断，原日志、源码和COMMAND_FAILED保留，
不能改写为整组通过。只修复该检查器边界：分别检查有限exp误差和
溢出后的实际FP32公式，并与固定CPU参考SiLU核对；加入损坏反例，
失败/中途退出也保存部分报告。补查只覆盖第28层和最终norm/head，
CPU120秒、0模型构造/0GPU，复用原捕获和前27层日志，原阈值不变。

## 原生音频codec与隔离接入

接着实现音频容器到16kHz F32单声道：WAV PCM/float、FLAC、MP3、
MP4/M4A AAC、Ogg/WebM Opus/Vorbis等使用显式demux/parser/decoder链。
复用视频worker的受信系统库加载与seccomp/线程额度，不启动外部
FFmpeg或Python服务；仅音频worker加载gstaudio和所需插件，视频
路径保持其原依赖集合，核心健康不依赖codec。缺失某codec只影响
相应格式请求，不能据此把格式实现任务从A10删除。

采用[系统audioresample](https://gstreamer.freedesktop.org/documentation/audioresample/index.html)
的Kaiser/quality10，限制滤波表；各通道重采样后以FP64求均值再转
F32，保持有限值，不暗中截断。编码字节、源采样率/声道、累计解码
样本、输出样本和时间分别有上限；实际时长来自输出样本，不伪造
对齐时间戳。输入仅来自父进程传入的冻结字节，worker无网络或文件
读取/写入权限。超时/取消/崩溃清理与未回收占槽复用既有机制。

CPU上限20项/180秒、0模型动作：实际WAV位深/浮点/多声道、重采样
与lossless/有损容器、有限值/损坏/容量/取消/依赖失能/崩溃/占槽；
受影响的共用加载逻辑另包含既有H264、VP8和sandbox直接回归。
合成编码夹具的离线生成程序不进入发行依赖。构建单列；有具体失败
仅修复并重验受影响项。随后继续资产生命周期和实际转录HTTP/SDK。

首组20项18通过、2失败：48kHz PCM24的重采样RMS门限以及AAC
输出长度。原程序、源码和失败记录保留。下一步只补这2项的实际
PCM/转换元数据捕获，60秒、0模型动作，先定位再决定修复；不改
原阈值、不重跑其余18项。夹具首次生成受本机裁剪版FFmpeg缺少
MP3 muxer限制，原现场保留，最终合成夹具以系统GStreamer明确
CPU编码链生成并固定摘要；两者均不成为q4t运行依赖。

实际PCM诊断显示：原强制插值表在48→16kHz留下约0.021个输出样本
的相位偏移，而混叠分量很小。改用1MiB阈值的AUTO表，对该简单
比例使用完整表，同时保留复杂比例的表容量上限；原1e-4门限不变。
AAC独立系统解码与worker逐位一致，原合成编码只含14336个可解码
样本，不能把期望的1秒当实际编码长度。夹具生成显式追加2048个
源静音样本供编码延迟/末帧使用，保留原短夹具；解码器不补零或
伪造原时长。后续夹具生成仅扫描固定CPU插件，避免普通注册表
扫描探测无关NVIDIA插件；原探测失败日志保留，无模型加载或GPU
计算；后续白名单路径已消除这类无关探测。

修复后仅6项/120秒：原2失败项、受表选择影响的Ogg/WebM、实际
容量和转换回执校验。其余身份/逻辑未变的检查复用，0模型动作。

该6项实际5通过、重采样仍失败，保留原件。输入PCM24转F32全
96000值与独立整数解码逐位一致，排除了源解码错误。检查固定
[GStreamer 1.24.2源码](https://raw.githubusercontent.com/GStreamer/gstreamer/1.24.2/subprojects/gst-plugins-base/gst-libs/gst/audio/audio-resampler.c)
发现FULL表仍可由默认cubic插值得到，单改AUTO并未绕开它；相位0
处cubic选择的系数表偏移与直接生成不一致。改为已有LINEAR插值
配置，保持Kaiser质量10和AUTO表阈值，不复制或修改系统源码。
追加只测受影响4项/60秒：原48→16k、Ogg/WebM和新44.1→16k、
8→16k的相位/长度用例（后两比例合为1项），原1e-4数值门限不改。

LINEAR配置后48→16k RMS为5.89e-8，44.1→16k为1.21e-5，均过
原门限；Ogg/WebM也通过。新增8→16k用例发现系统EOS排空只输出
15999个样本，原1秒源需要16000，原失败保持。修复为在已转换
F32的流尾向同一滤波器提供一个零延拓输入帧，只取原始源长度
对应的ceil输出长度；延拓不算源数据，不用人工补输出值。母进程
复核源/输出数量合同，并保留实际duration。新增非整比例尾样本
边界；此修改影响所有音频格式/容量路径，下一组18项音频CPU/
180秒一次完成受影响验证，已通过且未改执行逻辑的视频3项复用。

排空首轮18项13通过、5失败：EOS回调运行时source pad已经标记
结束，不能再向同一source pad推buffer；改为在原EOS事件到达前
向已连接的滤波器sink交付边界帧，再正常传递EOS。保留已有容量
故障，避免后续排空或bus错误覆盖其分类。原失败保留，只补5个
受影响用例/60秒，0模型动作。

最终5项0.564秒全部通过；公共加载器增加符号后又针对最终二进制
复核H264/VP8两项（0.064秒），原sandbox直接复用。累计
[21项独立CPU证据](evidence/serve-audio-codec-cpu-20261117/README.md)
包含58次执行/5.466秒及所有原失败。默认和day身份保持；继续
真实ASR资产生命周期、转录HTTP/SDK与恢复，不关闭A10或总目标。

## ASR资产加载与生命周期接入冻结

抽取embedding既有固定资产读取器，由ASR复用同一个隔离资产加载槽。
只接受编译期固定profile、文件名、大小和SHA256；先核对全部元数据
及tokenizer，再分配权重暂存，完整文件和612个tensor合同通过后才
发布对象。元数据模式不打开权重。父进程不做资产文件I/O；文件打开、
读取、页缓存回收提示在exec子进程中进行，未回收进程继续占同一槽。
暂存预算只约束原始字节，服务准入另计tokenizer/设备/文件页的统一RAM。
哈希增加分块取消检查；固定元数据解析仍是有输入容量上限的同步工作，
不是硬实时抢占。取消、超时、资源不足和依赖损坏分开记录。

本组最多22项CPU/180秒：ASR真实元数据、无权重模式、SHA/布局污染、
暂存预算、非法路径/文件类型、取消/超时/崩溃/未回收占槽与输出保持；
共享读取器的embedding直接回归及分块SHA取消。实际完整权重只读
验证另在严格租约下最多1次/120秒，无CUDA初始化、无模型构造/HTTP；
既有计算及codec证据复用。随后继续服务健康/冷却恢复与转录入口，
本组通过不等于ASR API或A10完成，不更换当前默认。

资产组实际18项全部通过，4.184秒：17项元数据/故障/embedding回归
加1次完整真实checkpoint读取；后者严格租约正常结束，0模型构造/
0GPU计算。首次独立host配置缺本机archive开发头路径，保留失败；
复用已经配置依赖的host构建目录，两次构建零警告。验后仅做空白
格式化，逐文件规范化内容相等，复用原程序与完整源码快照。
证据见[资产加载验收](evidence/serve-asr-assets-20261117/README.md)。
继续健康/冷却恢复和转录HTTP/SDK；暂存上限不冒充统一RAM预留，
实际模型/codec业务组合仍须对应短验，原计算差分FAILED继续保留。

## ASR03服务接入与验证冻结

继续原生转录接口和生命周期，不要求外部模型服务。显式配置ASR
资产目录、音频时长、上下文/输出与工作空间预算；健康状态单列
configured/loading/metadata_ready/ready/degraded，失败有5秒请求驱动
冷却，取消不消耗故障重试次数。辅助资产/codec失能不改变核心
readiness；真实CUDA失败仍交给既有设备保护，不能靠重载ASR掩盖。
模型和请求工作空间计入统一RAM与现有设备分配账，执行逐层交还
公共GPU锁，实际完成才记进度。输出达到容量或时限而未EOS时返回
相应错误，不发表正常完成事件冒充完整转录。

按[正式请求页](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
和[流事件合同](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/streaming-events)
接multipart、language/prompt、temperature、实际logprobs/usage及
JSON/text/SSE。Docs抓取器的create结果只有导航元数据，本次已打开
正式HTML补核；新keywords/languages和VAD/对齐/说话人字段逐项登记
实现任务，不按固定SDK缺字段而遗漏。逐token真实输出增量与UTF-8
边界一致；语言头仅作模型元数据，不能泄露为转录正文或假造概率。
时间戳/字幕/说话人需要真实对齐或相应后端，继续后续实现和实际
验收，不以接口拒绝作为功能完成。采样0的恢复策略需明确并验证，
不能忽略官方文档中的自适应行为或对尚无证据的阈值宣称等价。

本组先开发后统一最多36项CPU/300秒，覆盖生命周期/冷却/设备保护、
资源与配置，multipart/字段/模板及输出事件/UTF-8/logprobs/usage、
错误与取消；复用未改的codec/资产/数值证据。实际HTTP/SDK和
主轻profile恢复组合在代码可运行后冻结具体请求驱动，届时再占GPU；
不扩展五档性能/全模型质量或长稳。该组完成后继续其余音频和全部
master任务，不将一组接口交付当总目标终点。

服务候选目前接入JSON/text与独立转录SSE；完整表单解析保留
keywords/languages、chunking_strategy（JSON及SDK bracket字段）、
时间戳/字幕和说话人字段。后几类执行器继续后续实现，候选的
asr_executor_pending不是兼容完成。实际用户prompt及关键词/语言
提示走普通BPE，不能通过特殊marker注入结构token。语言提示按
已固定ASR模型的30种语言解释，不假造其他模型能力。

温度0的原生策略从贪心开始，每8个实际输出token观察平均原始
logprob，低于-1时升0.2，最多1；保留RNG/历史，无重播和隐含额外
生成预算。非零温度固定。此策略落实自适应语义，不宣称是OpenAI
未公开的内部阈值。采样mask排除未定义padding ID，但概率分母
保留真实完整head；转录logprobs与Chat不同的wire形状复用真实
token/UTF-8映射。JSON/text/SSE都只在实际EOS和清理后成功结束。

当前候选16项协议用例按实际登记冻结，不借用全套host测试；
健康10项及新协议/直接回归合计仍不超过36项/300秒。构建单列；
任何具体失败保留并仅重验受影响项。实际模型入口仍待短验后
决定交付，默认rev266和已交接day不变。

本轮实际选择34项CPU：10健康/资源/配置、16输入/输出/错误合同，
4项共享Chat概率/传输直接回归，4项真实ASR/embedding tokenizer
检查；精确名单保存在service-cpu-plan-01.json，严格不允许skip。
总300秒、0模型构造/0GPU；全部原codec/资产/数值证据复用。

首组34项CPU实际33通过、1失败：新literal编码路径达到token预算
后虽然返回错误，仍留下部分ID。保持失败断言，修复新模式为任意
编码失败都清空输出；原默认编码合同不变。只补这一项/60秒，
其余33项复用，原FAILED与完整源码/程序保留。引擎和原生入口
必要构建均已完成；本修复重建相关目标后再做实际业务。

## ASR03实际HTTP与SDK短验冻结

使用精确候选`c1cabddd487ccd564294983599a1e07fb5381a2d416b48c45e97c635234c6217`，
严格NVML租约中最多1次主模型启动、9次创建、600秒（预留40秒清理）。
保持主模型MTP/单流ctx8192；ASR显式启用，单次最多30秒音频/
128输出token，不改默认发行。音频使用已固定的官方zh.wav/en.wav，
由实际原生codec处理，不复用旧soxr特征冒充服务路径。

9次依次为：ASR权重缺失、冷却拒绝、核心Chat、Python SDK中文JSON、
JS SDK英文SSE、JS强制中文language/prompt的text、非法字段拒绝、
首个正文delta后主动断开，以及同PID核心Chat。资产故障只调整本组
私有目录的只读hardlink名称，原资产内容不写；恢复后不得重启模型。
核对ASR独立degraded/ready与主readiness、实际usage/logprobs和事件
终态、真实取消/槽回收及退出清理。中文精确匹配固定官方转录；
英文按保留英文词和撇号的小写WER<=0.05验收，同时记录是否逐字一致。
原数值logits FAILED不追认；无五档性能或全模型质量主张。

固定JS SDK7.47.0/Node24.18.0，新增隔离的Python SDK2.8.1仅作为
验收依赖，不进入q4t运行。系统venv缺ensurepip的准备失败保留，
改为without-pip环境由已有pip安装，未改系统包。依赖/程序/源码
与请求在prepare时固定；无自动重试。源码与CPU阶段已提交推送，
本次驱动另形成可审阅边界后执行。通过后继续服务包、原生TLS/
主轻恢复组合和其余ASR/全部master，不以本次短验作为总目标终点。
