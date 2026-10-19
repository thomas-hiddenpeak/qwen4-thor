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

## 四层精确父版本差分预算（执行前冻结）

父源码从73aab36 git archive导出至parent-source（仅增加相同独立
replay目标），候选f01e8fa；分别用各自头文件/库编译同一fixture，
不能拿默认B1a二进制冒充精确父。两侧q4t及replay零警告构建通过。
候选首次build在未重新configure时无target，记录保留；configure后
成功，不涉及运行时失败。

单lease运行父/候选各一次，均4层（含GDN、PLE、full attention），
max_seq=4、max_prefill=8、max_len=32。每侧5次prefill、4次packed
 decode（B=4/2/1/1，slot顺序2/0/3/1）；最后重置并复用slot2。
每步保存logits、完整recurrent、KV、raw/comp index、RoPE、host
cursor/history，共28文件，要求父候选逐位相同。通过范围限定固定
四层轨迹，不能宣称全模型多流质量或跨形状一致。

原模型全量hash清单复用10-19记录，运行前后核对每文件size、mtime、
ctime、inode、device；本阶段不声称重新读取全模型payload校验。
记录各binary/fixture摘要、源码差分；每进程600秒上限，无重试。
完整run_model_replay.py经gpu_exclusive持锁，model-replay-v1输出，
model-replay-lease-v1锁；环境冲突ENV_ABORT。模型加载总2、HTTP0。

## HTTP质量预算（执行前冻结）

四层父候选28文件逐位通过后，运行质量off/on各11条，总22条，
两个完整模型服务启动、每组1800秒上限、加载300秒上限、无重试。
使用tools/evalscope/run_acceptance.py --mode quality，max_seq1、
max_prefill8192、max_len208896、port8740，固定B1b已接受quality-off/on
的inputs/results副本，要求同模式prompt/output一致。候选binary从
本阶段build读取并保存副本，运行前后校验摘要。B1b参考仅作为已接受
文本质量对照，不冒充73aab36重新运行的精确父性能。

完整run_quality.py由gpu_exclusive持同一把锁，输出quality-v1，
lease为quality-lease-v1。失败立即停止后续组，不自动追加样本。
该阶段只验HTTP质量，非性能矩阵或多请求取消合同；后两者仍待
完成。新源码身份与默认部署身份分开，默认binary保持。

四层差分原始目录model-replay-v1，共67文件摘要复核通过，见
[审计](evidence/inference-metadata-20261020/model-replay-v1/audit.json)。
HTTP quality-v1已启动，正在持锁执行；未取得完整终态前不得标通过。

## 析构无法完成的host补验

审查发现原8例只覆盖Drain失败后恢复排空，未直接覆盖析构失败保留
源分支。新增1例mock：已提交后令析构wait失败，确认源和event未释放，
恢复mock后仍能读取原值，再由测试模拟context回收。生产类不改动，
仅测试驱动加--retained-source入口。host-v3不链接cudart，执行此新增
1例，不重复已通过8例；GPU新增0。属于具体覆盖缺口补验。
