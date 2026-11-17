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

## DI02特征组冻结

8CPU/180秒、0GPU：6组固定PCM（零、冲激、单频、多频、噪声、
非整hop）完整80维矩阵对官方固定源码的CPU参考，最大绝对误差
≤0.002；另2项覆盖采样率/长度/帧预算/非有限输入和前中末取消
的原输出保持。使用周期Hamming、中心零填充、完整末帧、HTK与
同左半带宽的对称三角、10log10及80dB裁剪/逐通道句均值。
不能复用ASR的Hann/reflect/Slaney或删除末帧。通过后继续ECAPA
实际计算和说话人业务；仅特征通过不完成DI02或A10。

## DI02实际计算开发组

原始权重保持F32，复用现有CUDA/cuBLASLt；使用FP32 pedantic矩阵
乘法，不以TF32/BF16替代。实现reflect卷积、TDNN/BatchNorm、三组
SE-Res2Net、层拼接、带全局统计的注意力池化和192维输出。按真实
阶段完成同步推进进度；CUDA错误与普通合同/资源错误分开传递。
GPU工作区由请求所有者供给，包含特征、卷积临时、全部中间值和
BLAS空间，按统一RAM计入资源预算；不暗中新增无界缓存/分配。

首组六项布局/绑定CPU、180秒，0GPU：精确张量映射、坏布局与
地址溢出、工作区非重叠及对齐、长度/预算边界、失败owner保持。
必要CUDA编译单列；实际数值与语音GPU短验在驱动完成后另冻结。
不以本组六项完成宣称speaker计算或接口已通过。

## DI02实际模型首轮短验冻结

先5项CPU验收器正负控/60秒，0GPU；随后同一严格NVML租约最多
1次ECAPA GPU加载、1次固定上游CPU加载、4输入、600秒（native
180秒、reference240秒），0HTTP/主模型启动。输入为640样本静音
（5帧）、固定随机种子30秒上限（3001帧）和官方两段真实语音。
逐帧80维特征对照max_abs≤0.002；七阶段保持同一native特征操作数，
每阶段relative_l2≤0.0005、cosine≥0.999999且max_abs≤0.02；最终
192维relative_l2≤0.0001、cosine≥0.99999999且max_abs≤0.01。
记录全部36项，不只看最终相似度；8输入/状态边界及每例4生命周期
检查一并固定。上游仅AST去掉无关导入，不改前向定义；完整单语句
均值与无autocast保持，原分类器/global embedding norm不启用。
本轮只验证计算合同；不是已知/未知说话人业务、聚类或API验收。

## DI03独立资产/资源/健康组

原生speaker复用现有独立model-assets进程与单槽，新增固定目录档案，
metadata先验，权重固定SHA和布局；取消/错误保持旧owner。普通资产/
worker/资源/计算错误仅降级speaker，CUDA设备故障保持共享保护。
预算包含原始文件页/暂存/设备副本，并保证最长10秒已知参考也可
在同一workspace执行；不把CPU复制当容量卸载。

冻结12CPU/180秒：6speaker资产（metadata不打开权重、同大小坏哈希、
错误profile/符号链接、取消/期限/预算、worker坏协议与未回收占槽、
真实83MB只读加载），4资源/健康，复用共享读组件的2原ASR保护。
本组0GPU加载/0HTTP；因含完整权重读取，整组沿用测试入口的严格
独占租约约定，监控异常保留ENV_ABORT，只清理自己的CPU worker。
必要CPU/运行时构建单列；不复跑已通过的ECAPA数值或主模型矩阵。

## DI03分段、聚类与输出组

基于实际VAD区间规划有界重叠声纹窗口；强制资源切片的连续区间
重新连接，静音间隔不补成声音。每个窗口负责相邻中心中点之间的
区间，声纹余弦距离作平均连接聚类；低相似度的不同声音不能因
max_speakers而强并。已知参考只在当前请求匹配，重名/冲突参考
拒绝，不建立跨请求身份缓存。未知标签按第一次声音出现顺序分配，
避开用户已知名称。默认阈值属于待真实语音验证的原生策略，不能
宣称是OpenAI内部算法或已达到业务准确率。

词标签来自实际对齐时间与声音区间的最大交集；原文字节、标点和
零时长词保留，未覆盖的时间不凭文本猜测。JSON与segment SSE用
同一已完成对象；最终成功需所有分段/实际usage完整，失败保持原
输出。重叠声音检测/分离仍须实际实现，不用本组单轨聚类替代。

冻结14CPU/180秒、0GPU/HTTP：窗口边界/强制拼接/静音/预算；
同人回访/异人/连接准则/已知参考/歧义与容量；原子失败与取消；
真实时间/中文标点/JSON/SSE/usage与跨片段累积。这里的声纹输入
为可核验构造向量，只验算法与协议；真实语音及完整API随后固定
短验，不复跑已过ECAPA数值或全模型矩阵。
