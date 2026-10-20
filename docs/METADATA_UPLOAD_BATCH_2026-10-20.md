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
