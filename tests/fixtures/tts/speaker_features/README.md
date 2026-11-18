# Qwen3-TTS Base说话人前端参考

六份合成24kHz PCM与128通道log-mel来自固定官方Qwen函数和librosa
0.11.0原函数，CPU-only PyTorch 2.14.0，未构建模型或初始化CUDA。
使用反射384、FFT1024、hop256、周期Hann、Slaney面积归一化、
幅值epsilon1e-9和log下限1e-5。原生C++不依赖Python或librosa。

[生成器](../../../../tools/native_tts/speaker_feature_reference.py)只提取
完整计算函数，去掉缓存装饰器与无关导入；版本与每个文件SHA见
[manifest](manifest.json)。包括短反射边界、噪声、多频和非整hop尾部，
比较完整矩阵最大绝对误差1e-4，不以均值或余弦隐藏局部误差。

Qwen源码Apache-2.0，项目语音参考许可记录见相邻模型来源文档；
librosa生成参考使用ISC许可，全文见[LICENSE](LIBROSA-LICENSE.md)。
这些是合成计算夹具，不表示音色相似度或业务质量已经验收。

首次与官方FP32逐log值比较的1e-4门有3例失败，原件与失败均保留。
逐阶段诊断确认低能量FP32 FFT舍入，滤波器一致。原生改用FP64
中间计算（仍输出FP32）；新增`--float64-equations`独立对照，仅
将官方函数的固定`.float()`改为`.double()`，所有方程不变。
canonical各完整矩阵仍要求误差≤1e-4，同时对原官方FP32结果保留
最大差异并要求舍入到BF16后每项≤1 ULP。BF16只是官方常用
权重/加载精度的比较量尺，不能替代原生FP32网络与完整业务验证。
`canonical-manifest.json`记录新夹具，旧`manifest.json`原样保留。
