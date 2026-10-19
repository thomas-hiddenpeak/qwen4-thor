# 普通多序列decode上传所有权候选

状态：DESIGN_FROZEN，尚未集成/验收。

本候选类型是运行时所有权结构整理，不以合并H2D或提速为目标。
保留现有GPU字段地址、3个批量H2D和每token三次RoPE H2D、计算形状、
kernel和服务完成边界。仅把ModelDecodeBatchMulti的token/position/
slot/RoPE上传源复制进模型持有的连续host缓冲；本步上传后记录事件，
下次填充前等事件，错误路径排空。GPU持久状态不进对象。

先独立实现DecodeMetadataUpload；显式容量、有效行数、目标字段，
不在此步骤修改Model或默认构建目标。对象不允许并发调用；与现有
model_mu独占scratch约束相同。跨stream源复用靠上一次上传事件，
目标的计算依赖仍由调用方负责，不能把“上传完成”当作“模型完成”。

资源初始化失败清理部分资源；提交失败后等待已提交上传，失败对象
不得再次写入。若CUDA不能确认完成，析构保留可能在途的host源并报告，
不为回收小缓冲而释放仍可能被读取的数据。此限制与正常析构分开测。

验证阶段尚未执行：先编译零警告，冻结独立对象测试的详细用例与GPU
预算；测试须覆盖延迟消费、调用者立即修改、同/跨stream重复上传、
不同有效行数、RoPE偏移、无效请求不变性与析构。错误注入在host
测试中验证状态机，不能通过破坏GPU上下文来模拟。

对象通过后才集成一个入口；运行时直接差分及HTTP质量/取消回归
预算另行冻结。性能若要声明保持仍需五档矩阵；不把本结构整理
候选改名为性能优化或提前宣称性能通过。当前新增GPU预算0。

现有普通decode的pageable源没有复现数据损坏，因此该项是显式
所有权加固，不标“修复已复现数据错误”。其他入口审计仍须完成。

## 对象host故障注入预算（执行前冻结）

链接包装CUDA API，执行实际DecodeMetadataUpload源码，不初始化GPU。
固定8例：host分配失败、event创建失败、第1/4/9次copy失败、event
record失败、completion失败后禁止重用及恢复排空、调用方改写后
正常析构排空。检查未完成源不被覆盖、部分提交排空、失败后不再
提交、初始化失败资源回收及正常析构回收。仅模拟CUDA API失败，
不声称重现设备故障或GPU时序。一次host进程，无自动重试。
编译C++23 -O2 -Wall -Wextra -Werror，产物candidate/host-test；
命令、build.log、run.log原样保留。GPU预算仍0。

host首轮工具失败：CUDA C++ cudaMallocHost实际调用cudaHostAlloc，
漏wrap后触及真实runtime，cudaFreeHost mock拒绝未知指针并abort。
该次没有GPU独占锁，不作为GPU/host通过证据，原binary/日志保留。
修复后host-v2不链接cudart，ldd确认无CUDA依赖，8例全部通过。
此为具体工具失败后的受影响重验，不变更生产候选。

## 独立对象GPU预算（执行前冻结）

一次进程，四次成功Upload（行数2/1/2/1，stream 0/0/1/1），共30次
H2D；1次非法slot拒绝不得改目标。每步前置50ms host callback，
调用者立即改写输入，连续两次上传检验源复用等待；跨stream使用
不同目标缓冲，不虚构同一GPU scratch跨流并发安全。最后一次依靠
析构完成。216个int与独立host期望逐位对比，包括未写区域-1。
无模型、无kernel、无HTTP；60秒timeout，无自动重试。零警告构建。
整个run_ownership_gpu.py经gpu_exclusive执行，输出candidate/gpu-v1，
锁为candidate/gpu-lease-v1；竞争/监控失败ENV_ABORT、保留现场。
仅对象所有权直接合同，不作整模型质量或性能接受。

## 单入口集成范围冻结

对象host-v2 8项与GPU 4次Upload/216整数全部通过，开始单入口集成。
LoadModel在分配d_seq_id后初始化同容量上传owner，Free先排空owner；
ModelDecodeBatchMulti通过owner上传原有三个字段及RoPE，随后原样
进入Embed/Expand/RunLayers。原同地址、同形状、同拷贝次数保持。
不接入其他入口，不删除verify的finish，不改变服务model_mu/通知。
新增空owner拒绝用于识别不完整Model；这是结构加固候选，不宣称
现有pageable数据损坏已复现。候选初始代码父版本73aab36。

独立构建在.q4t-work/inference-metadata-20261020/build，默认build/q4t
不替换。此时只构建，模型/GPU集成测试预算仍待完整判定卡冻结。
独立对象测试不能代替集成数值、HTTP质量/取消与性能证据。

## 当前交付状态

对象直接合同已通过，候选已集成单入口并完成独立零警告构建。
[证据与工具失败](evidence/inference-metadata-20261020/ownership/audit.json)、
[构建身份](evidence/inference-metadata-20261020/ownership/candidate-build.json)。
模型接入、HTTP质量/取消、性能均未验收；默认binary保持。
前述“尚未集成”是设计冻结时状态，由本段更新。
