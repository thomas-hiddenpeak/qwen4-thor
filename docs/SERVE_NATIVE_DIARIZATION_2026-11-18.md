# 原生说话人分离实施合同（DI01—DI04）

继续A10，不把diarized_json的503视为完成。先固定SpeechBrain
ECAPA-TDNN-1024声纹embedding的官方权重和实际计算，复用现有
16k解码、VAD、ASR与实际对齐，形成声音分段/聚类/已知参考的真实路径。
声纹只作本请求内标签和明确提交参考的匹配，不接外部身份数据库。

DI01固定模型revision 0f99f2d0ebe89ac095bcc5903c4dd8f72b367286，
源码c0241278f93ef02cd5191f03caf4e5f8e1292bf6，Apache-2.0。
原始embedding_model.ckpt为83,316,686字节，SHA256
0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2。
资产和转换合计上限200MB、600秒，至少保留6GiB可用磁盘；本次
是小型独立资产预算，不修改前期两个约2GB资产的8GiB要求。
原始资产核对LFS SHA；CPU weights_only转换至严格safetensors，
保留名称/shape/dtype/位模式，运行服务不读取pickle或引入Torch。
首组8CPU/180秒：固定结构、tensor总合同、非法shape/dtype/区间/
重复/未声明张量及实际checkpoint核对、取消/失败owner保持。
本组0GPU，必要构建单列。仅组件通过不宣称接口可用。

DI02原生80维Fbank/句均值、ECAPA卷积Res2/SE/统计池化/192维
向量；使用固定官方实现作局部与真实音频差分。不使用ASR的128维
特征替代；不混入分类器7205已知身份输出。实际运算路径确定后
冻结针对性数值与真实不同/同说话人清单，GPU动作先固定独占预算。

DI03实现实际切换检测/聚类与已知参考匹配，默认未知按首次出现
A/B排序；边界来自音频，词归属使用真实对齐。参考解码、2—10秒
和4人上限/格式、usage、取消/资源/独立故障恢复均接入。重叠语音
需要真实分离/检测路径，不能硬指定一个speaker冒充重叠已解决。

DI04原生JSON/SSE与双SDK、同人/换人/回访/已知参考/静音实际
业务、主轻/TLS/恢复组合、可回退交付与主线。复用未变证据，
不做完整性能或主模型质量矩阵。通过后继续Chat音频、合成、
Realtime及全部master，用户负责长稳实跑。

依据：
- https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb/tree/0f99f2d0ebe89ac095bcc5903c4dd8f72b367286
- https://github.com/speechbrain/speechbrain/tree/c0241278f93ef02cd5191f03caf4e5f8e1292bf6
- 本地固定OpenAI Python SDK的transcription_create_params、
  transcription_diarized、transcription_diarized_segment合同。
