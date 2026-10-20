# 剩余元数据上传源：批次所有权方案

状态：DESIGN_FROZEN_OBJECT_STAGE。父版本d6bb9a8，当前部署c8f4b241
保持；这是预防性来源寿命加固，不声称复现旧模型pageable损坏。
目标覆盖RunPrefill、ModelDecodeStep/Batch、ragged prefill/mixed和
MTP chunked extend；保留verify既有finish及已接受multi owner。

## 对象合同

MetadataUploadBatch持有pageable字节块及一个完成事件。Begin先排空
前批次再允许重填；Copy把调用方内容复制进独立稳定块后提交原目标
H2D；Finish在最后上传后记录事件。不增加pinned内存，不更改GPU
地址/计算形状或拷贝粒度，尤其保留单序列batch的三次连续RoPE上传。
块按拷贝序号复用容量，不能在未完成时增长或覆盖旧块；增长的块
和描述符容器分配失败必须返回错误并保护已提交源。

中途返回未Finish的批次仍由owner持有，下次Begin/析构用原stream
排空；不能因scope退出而释放在途数据。提交/record/完成失败后
对象失效，禁止后续重填；排空失败保留源。析构仍无法确认完成时
有意保留源块和事件，报告错误，不提供GPU目标寿命或设备恢复保证。
调用方继续负责GPU scratch消费者串行化，stream必须活到排空。

代价是host快照复制、可复用pageable块、一个事件及每批次完成检查；
不宣称提速、统计持平或代码总量下降。相比每次小H2D后同步，不在
每个字段上加入等待；相比全面pinned迁移，不引入额外锁页配置。

## 本轮实施与验证预算

先实现独立对象，不接入Model/MtpModel或默认构建。纯host mock
直接链接实际对象源码、不链接cudart，模拟延迟读取源，共10组：
调用方覆盖；多个稳定源块；同/跨stream批次复用；未Finish提前退出；
event创建失败；第2次copy失败；record失败；completion失败禁止重填；
析构排空；析构失败保留源。参数拒绝并入对应组。
编译g++-14 C++23 -Wall -Wextra -Werror，构建产物仅.q4t-work；
一次执行、不自动重试；首次失败保留，只针对具体修复重验。

本轮模型加载/GPU/HTTP预算0。host对象通过后，冻结一次独立真实GPU
延迟读取验证，再按上述入口成组接入、冻结真实入口直接/数值及HTTP
回归。旧对象/HTTP不能冒充新身份验证；不自动追加性能采样。
单步seq_id边界缺口独立登记，真实入口检查在接入前补齐验证卡。

## 对象host结果与GPU验证卡

batch-host-v1零警告构建，ldd无CUDA runtime，10组全部通过；构造
completion失败时的stderr是预期故障注入，不能当真实设备故障。
本轮没有触及模型runtime或已部署binary。host分配失败有处理分支，
本组未注入该分配失败，不把10组扩大为所有异常分支验证。

接下来仅一次GPU进程，4批次×2次H2D，元素数8/4/3/2；stream为
0/0/1/1，第一批刻意不Finish检验早退后排空，第二/三批Finish后重用，
最后一批由析构排空。每批前置50ms host callback；调用方立即覆盖
原数组。不同stream使用独立目标，不宣称scratch跨stream消费者安全。
非法Copy不得改目标；目标未写尾部保留-1，合计检查64个int。
一次60秒超时、无重试；编译-Wall -Wextra -Werror，单NVML lease。
模型加载/HTTP0，非性能测试。产物batch-gpu-v1，guard终态后封存。

## 对象阶段结果

10组host合同与一次真实GPU检查均通过，GPU 4批次/8次H2D/64个int
逐项一致，guard COMPLETED/exit0，零警告构建。未接入Model/MtpModel
或CMake默认目标；默认binary保持c8f4b241。对象证据见
[审计及身份](evidence/metadata-followup-20261020/batch-object-v1/audit.json)。

下一步先冻结单步/单序列batch/RunPrefill与ragged/MTP分组接入卡，
保留已有拷贝粒度和GPU调用顺序；补真实入口边界与错误返回覆盖。
对象检查不能替代模型差分、取消或HTTP；新的runtime身份不部署，
直到其相应验证和接受明确。已有统计性能方法结论不变。

## 模型接入冻结（实施前）

父身份48185ed（运行时等同已部署3d5564e），复用main，候选独立构建
至.q4t-work/metadata-followup-20261020/build；默认binary不重建。
Model和MtpModel各持独立MetadataUploadBatch，Free先销毁owner。
事件延迟到第一次Begin创建，不增加Load时CUDA事件。主模型覆盖
RunPrefill、ModelDecodeStep、ModelDecodeBatch、ModelPrefillBatch、
ModelMixedBatch；MTP仅接入chunked MtpDraftExtend的ids/pos/seqid。

每个入口Begin、按原顺序Copy、最后一项元数据上传后Finish；不合并
拷贝、不改目标、不增kernel。RunPrefill为保留原Embed/多模态注入
顺序，Finish在可能的image位置上传/注入后、Expand前，因此事件
也覆盖此前同stream已入队的Embed/Inject；不能称纯传输计时事件。
任何未Finish早退由owner继续保活；MTP临时device释放前先Drain。
已接受DecodeBatchMulti和ModelVerifyMulti不改。

入口防护单列：ModelDecodeStep在rope_delta索引前检查seq_id；
ModelForward在Reset前检查seq_id；RunPrefill检查T、seq_id及容量，
防止内部入口依赖不可见前提。ragged累计计数使用减法检查避免
int溢出；Mixed范围检查避免base+len溢出。这些是同接入路径的
安全前置，不变更调度和分配策略。

复查纠正：最初拟检查ragged ResetAllLayers返回值，但该helper始终
返回成功，底层DecoderLayer::ResetState为void；只加调用处判断不
能证明错误传播，已在真实入口测试前撤掉无效改动。复位错误传播
需要单独贯穿底层API，本轮未改，不将上传失败保护扩大为复位安全。

验证分组：

1. 真实入口纯host边界：固定无模型用例覆盖非法seq_id、超容量、
   Mixed位置溢出及ragged计数溢出；合法单步/单batch到缺owner截止。
   具体用例随测试源码保存，最多20次函数调用，一次执行，保守持锁；
   不加载模型/执行GPU，不把mock对象测试替代入口检查。
2. 构建候选及精确父版本的同一差分夹具，保持4层、max_seq4、
   max_prefill8、max_len32。正式GPU采样前另冻结具体轨迹与文件比较
   清单，必须覆盖prefill/step/batch/ragged/mixed/多模态位置和MTP
   chunked，不能只重复原DecodeBatchMulti夹具。此项当前执行预算0。
3. 直接合同通过后统一对应HTTP质量与取消回归，冻结新身份后执行；
   当前执行预算0，不在每个中间源码版本上反复加载。性能新增0。

旧对象测试按源码未变复用；接入不能继承旧完整模型验收。新候选
在上述验证完成前仅作阶段快照，不部署、不宣布全部入口完成。

## 接入构建与入口检查结果

主模型5个入口及MTP chunked路径接入完成。独立候选binary
753d9769f96e9848800eae88b4800bef65e20885fd2ec025b8f3bc91fc497542，
完整构建及撤掉无效reset检查后的增量构建均零警告；19项真实入口
检查全部通过，完整guard COMPLETED。没有加载模型或新增HTTP。
证据见[接入身份和结果](evidence/metadata-followup-20261020/batch-integration-v1/result.json)。

默认c8f4b241不变；已通过的批次对象源码未变，其host/GPU对象证据
复用。新接入候选仍待精确父版本差分及HTTP，不能据19个拒绝/缺owner
检查宣称正常模型路径通过。新增检查包括seq_id拒绝、INT_MAX累计
和Mixed位置相加溢出拒绝、合法单步/batch到缺owner检查即停止。

复核剩余MTP Async：推理multi-step的seq_of/h_pos/gather_off/ext数组
仍由既有step scope和cleanup同步保护；单序列路径使用同步H2D，
未因统一风格迁移。LoadMtp内load lambda的临时host权重仍依赖pageable
行为，它是加载期权重而非本轮推理元数据，不声称被本改动覆盖。
同样，完成失败后的GPU可恢复性与ResetState错误传播仍不是本轮保证。

批次pageable块保留按拷贝序号观察到的容量上界直到Free；额外host
驻留取决于实际布局/配置，不是零成本。此内存与event/快照复制
成本不能被解释为已获性能接受；本轮不追加统计性能样本。
