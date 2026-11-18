# 原生语音输出、Chat音频历史与实时接续

继续A01/A02/A07/A09/A10/A12与可靠性/交付总任务。rev306已验Chat
语音输入不能替代实际语音输出。本组先完成真实TTS后端，再接标准
speech/Chat音频输出与历史，Realtime继续使用真实输入/输出组件。
不增加外部TTS服务，不用文本回显、静音WAV或格式占位替代合成。

## 固定来源与实现选择

官方Qwen3-TTS源码固定`022e286b98fbec7e1e916cb940cdf532cd9f488e`，
[Apache-2.0](https://github.com/QwenLM/Qwen3-TTS/blob/022e286b98fbec7e1e916cb940cdf532cd9f488e/LICENSE)。
[0.6B CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice/tree/85e237c12c027371202489a0ec509ded67b5e4b5)
与[1.7B CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice/tree/0c0e3051f131929182e2c023b9537f8b1c68adfe)
分别固定上述revision，共享同一12Hz codec权重SHA
`836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258`。
0.6B主权重1,811,626,576字节，1.7B主权重3,833,402,552字节，
codec682,293,092字节。仅先获取元数据/头部，未将Range头部检查
冒充完整权重SHA验证。所有新资产只写.q4t-work，既有模型目录只读。

官方推理代码对0.6B明确忽略instruct；本项目不能照搬这种静默丢失。
因此原生计算同时建模两个形状，完整指令合成使用1.7B的实际能力；
0.6B可作为明确能力合同的基础合成选项，不能被当作无损自动回退。
可参考只读qwen35-thor `57e29777c2aff8a97f42df6e3d9487b1327f014f`
的MIT实现；先核对本模型差异，实际移植时保留版权及许可。核心不
依赖Transformers/PyTorch，参考环境仅用于离线数学/实际音频核验。

## 连续子目标

1. **TTS01** 固定两种talker/预测器及codec的配置、token/voice映射、
   dtype/完整权重区间和读取预算；实际资产全SHA后才能加载。
2. **TTS02** 原生talker、15级code predictor、合法终止与采样、文本
   和指令条件；进度/取消/时间及统一RAM预算贯穿每次真实计算。
3. **TTS03** RVQ、因果卷积/转置卷积、codec Transformer与真实24kHz
   PCM，分块边界和实际样本数；不得将生成codes冒充音频成品。
4. **TTS04** `/v1/audio/speech`的真实voice/format/speed/instructions、
   HTTP与流式、用量/错误/取消、模型登记和独立辅助故障隔离。
5. **TTS05** Chat modalities/audio输出、JSON/SSE、真实audio id历史
   回填与过期/持久隔离；回答文本、转录和输出音频来源必须一致。
6. **TTS06** 双SDK、原生TLS、主轻/版本恢复和可回退发行/主线；
   后端不兼容时不得丢音频能力或自动切到不接受指令的模型。
7. **TTS07** 接续Responses/Batch/Realtime相关合同及实际业务；
   与其他master剩余项一起完成，前述任一阶段不结束总目标。

## 本组有界验证与存储准备

先完成TTS01配置/布局的8项CPU/180秒：两种官方配置、计算参数/
重复键/未知覆盖拒绝、voice/language/特殊token/采样默认、完整talker
权重图及其差异、codec计算配置、dtype/形状/区间/缺失/重叠拒绝。
只使用冻结元数据，明确不代表完整模型验证，0GPU/HTTP；必要构建
单列。真实资产完整校验、数值和HTTP在相应实现完成后分别冻结，
不提前加载模型、不跑全模型质量或五档性能。

当前磁盘约3.3GiB可用，不能直接下载完整1.7B与codec。先核验旧的
可重建编译缓存与可恢复归档，保留程序、源、模型、原失败和验收
证据；空间不足不降低完整资产检查。源码/CPU开发可独立继续。
默认rev306、原重叠识别质量失败及全部总任务保持。

TTS01的8项元数据CPU全部通过（0.032秒），独立构建零警告。
补齐来源清单的私有音频头文件，863锚点/10负控通过；原失败保留。
完整1.7B资产开始下载，固定4,520,217,432字节、5400秒上限，始终
保留至少1GiB可用磁盘；逐文件校验Git blob/LFS SHA256，不加载模型。
旧CMake `.o`缓存共12,867项已按冻结清单清理，释放2,641,846,272
分配字节；源/程序/静态库/模型/默认与证据保持。两次归档估算超额
均在删除原件前退出，只有随后单列的可重建对象缓存执行了清理。
没有将这次清理描述成归档成功，完整清单及CMake重建路径保留。

完整1.7B与codec的12资产已在475.826秒内全部核验，4,520,217,432
字节，完成磁盘余量1,568,165,888字节；与已验元数据配置/权重图
一致。[资产记录](evidence/serve-tts-assets-20261118/README.md)，0GPU。
继续真实计算、合成与接口，完整资产身份不能替代功能验收。

TTS02先冻结原生执行的CPU入口：tokenizer与官方16文本样例、
指令/文本双轨prompt、语种/方言、完整权重绑定、KV与临时工作区
预算、code采样和逐帧状态；连同直接受影响的ASR/主模型tokenizer
3项，共最多18项CPU/180秒，0GPU。GPU数值在计算实现完成后
单列冻结，之后继续PCM与真实服务，不以本组合同通过结束。

TTS02入口16唯一CPU已通过，原无首行merges失败保留，只补3
失败项；累计19次执行、2.180秒，两次构建零警告。实际模型
CUDA/PCM/服务仍在继续，不能将入口合同当作功能交付。

TTS02 CUDA必要构建已零警告通过，新增完整权重绑定和prompt缓冲
两项CPU/0.008秒通过。固定计算短验：同一严格租约内1次GPU talker
加载、1次CPU官方参考加载，中英文各2帧、合计4次talker及60次
预测器参考forward、0主LLM/0HTTP，整体1200秒、各侧600秒。
比较132个实际BF16操作数及两组完整codes；投影/提示cosine至少
0.99995、relative L2至多0.01，hidden/logits至少0.9995、至多0.04，
所有teacher-forced贪心codes须一致；不会将2帧截断宣称EOS或语音
质量通过。先3比较门负控CPU/60秒及来源门，再执行冻结数值计划。
官方CPU参考只执行原始talker/预测器及其提示准备；未用声纹提取和
codec加载均设显式抛错入口，不能被冒称已执行或合成资格。

TTS02原BF16首轮数值FAIL保留，35同输入GEMM高精度复算未发现
累加/舍入界外误差，定位为逐层舍入经敏感注意力放大。尚未交付
的新后端改FP32执行，保持BF16源权重全部信息和公式/采样；关闭
TF32，权重7,666,705,408字节与翻倍KV均计RAM。固定7C++与
3比较门CPU/180秒后，同原2例各2帧、1GPU/1CPU加载/1200秒；
条件cos≥0.999999999/relL2≤1e-5，hidden/logits cos≥0.99999999/
relL2≤1e-4，全部64贪心code相同。原BF16记录不改判；F32通过
也不能代替后续EOS/PCM/实际业务验收。

TTS02 FP32计算短验已过：10CPU/零警告构建，实际4帧45.295秒，
132操作数及64 code与官方参考满足严格合同；租约释放、默认
rev306保持。仅两帧截断不覆盖EOS/语音任务，继续TTS03—07。

TTS03先固定4项CPU/60秒、0GPU：完整F32 decoder绑定与
vocoder/dilation，统一RAM/固定卷积tile上限，300帧/25左上下文
及尾块1920采样/帧裁切，GPU前排除EOS等非音频code。codec
实际计算/音频数值和声音质量在接线后单列，不复跑已过talker。

TTS03完整codec已零警告构建，固定3比较门CPU/60秒后，同严格
租约1GPU/1CPU codec、3例5次forward/155输入帧/218880采样、
1200秒。2帧来自已验talker；75帧和37帧为固定合法数值夹具，
覆盖72滑窗及16帧块/25左上下文裁切。90前后缀操作数及3完整
PCM均要求cos≥0.999999、relL2≤0.001、有限且样本计数精确。
复用talker通过证据，0主LLM/HTTP，不将数值夹具当语音质量；
通过后继续完整EOS/实际内容及TTS04—07。

## Speech输入与输出合同补齐

2026-11-18核对[官方Speech接口](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)：
input/instructions按4096个Unicode字符，model/voice必须保留；
voice既可字符串，也可自定义id对象。输出含mp3/opus/aac/flac/
wav/pcm，speed为0.25—4，stream_format为audio/sse。自定义
voice的真实资产/注册/条件后端仍是待实现项，不能只解析后换成
固定说话人，也不能删除该能力。PCM/WAV原生无编码库依赖，
压缩格式复用既有隔离媒体worker；库/插件故障仅影响对应格式。
固定本组5CPU/60秒，随后实际语速/编码/HTTP/SDK与恢复；已验
合成数值与两句真实发声直接复用，不扩五档性能或全模型质量。

## Speech变速与编码的当前实施状态

0.25/2倍的原中英文内容通过；4倍直达与两级候选仍有丢词，
独立SoundTouch对照及减速回放不能排除算法内容损失，仍需
解决，不能以缩小speed范围或“参考也失败”结案。
[原失败](evidence/serve-speech-tempo-failure-20261118/README.md)和
[后续诊断](evidence/serve-speech-tempo-diagnosis-20261118/README.md)
保持可核验。该库仅用于测试，不进入生产发行。

六格式编码成组实现，压缩格式复用GStreamer受限子进程，
共享文件解析容量槽，样本/输出/线程/CPU时间及父进程墙钟截止
分别有界；缺库、崩溃、卡死与取消只影响相应请求。PCM/WAV
直接原生输出。当前版本接完整PCM，生成中流式编码仍须完成，
不能把完整生成后分块发送替代实时流式。固定9 CPU/60秒验证
实际格式与回环、故障隔离和回收，随后实际语音、HTTP和恢复。
参考接口为[MP3](https://gstreamer.freedesktop.org/documentation/lame/index.html)、
[AAC](https://gstreamer.freedesktop.org/documentation/voaacenc/index.html)与
[Opus](https://gstreamer.freedesktop.org/documentation/opus/opusenc.html)。
只使用库的公开API，无移植这些项目的实现代码。

六格式组件10项独立CPU已过，见[编码证据](evidence/serve-speech-encoding-cpu-20261118/README.md)。
AAC真实末尾截断已补整帧和两帧编码排空，单列填充采样量；
压缩实际语音、生成流式/HTTP/恢复及4倍内容仍继续。

四种压缩格式的真实中英文8例全部原文匹配，见
[压缩内容资格](evidence/serve-speech-encoding-content-20261118/README.md)。
16个CPU编解码worker回收，3 ASR加载/10.644秒，同租约释放。
继续TTS04/05服务资产、资源预算、生成中流式与协议；现有完整
PCM编码不能替代流式。4倍内容保留待修，总计划仍active。

## 服务资产与资源生命周期

TTS服务资产/资源成组实现：两个固定资产集原子发布、共用截止、
聚合预检；F32设备权重/原始暂存/页缓存/6MiB展开与请求工作区
均计统一RAM，独立TTS健康保留真实设备故障。固定11CPU/180秒、
0完整权重读取/0GPU，之后严格租约验证完整读取，再接真实服务。
为存储余量无损归档2217个参考Python字节码缓存，源/模型/程序/
证据不变，恢复清单保留；全部master与4倍内容问题继续。

CPU检查覆盖9项新资产/资源/独立健康及2项直接受影响的ASR资产
合同；不加载完整权重，不跑完整性能/数值矩阵。运行时接入、
模型真实加载和恢复另有针对性短验，组件资格不代表服务已交付。
