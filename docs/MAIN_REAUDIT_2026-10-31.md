# main 修复后复审：回归与遗漏

审计锚点为`802bd70559b5e6d4f4d3b3ede5eaeac3bda90d09`，复核了
A1～A8改动、相邻的权重消费者、专家状态、加载线程和发布进程回收。
**未确认本轮修复新引入的回归；确认4项P2既有遗漏，尚未修复。**
四项均在修复前`c26e97a`与当前main上复现，不据此撤销原8项的
限定修复结论，也不把通过的正常路径验收扩张为所有异常路径安全。

## 发现

### R1 · P2 · 专家回退读未校验目标张量大小，可能污染权重或越界

位置：[ReadExpert固定缓冲区读取](../src/quant/moe_residency.cpp#L783)、
[标量读取](../src/quant/moe_residency.cpp#L773)；全驻留路径的
[LoadMoEWeights](../src/quant/moe_weights.cpp#L159)也有同类无尺寸读取。

Safetensors现在能证明文件内的shape、extent和边界自洽，但不能证明
某个张量适合调用者提供的缓冲区。ReadExpert将尺寸不匹配视为无法
快读，随后通过没有目标长度参数的ReadTensor读取完整张量；此处
仍使用根据模型维度划定的固定权重区和4字节标量槽。

CPU合成分片中，预期4096字节的down weight实际只有2048字节，
Init和Resolve均成功，设备替身的后半段保留初始化哨兵0xA5；另将
down input_scale设为合法F32[2]，仍返回成功，并改写pinned池末尾
之外4字节。两种分片的文件内extent均合法。探针为分配额外增加64
字节保护区，避免真正破坏进程堆；生产分配没有该保护区。

影响条件是权重与模型维度/标量合同不匹配，不声称当前已验收模型
存在此损坏。建议在形成任何快读/回退描述符之前验证必要张量的
dtype、shape及精确字节数，或增加带期望尺寸的读取接口；所有
分支必须在写目标前拒绝不匹配，覆盖短张量、长张量和标量宽度。

### R2 · P2 · 热专家列表的重复项占用多个槽，导致有效容量误判

位置：[InitHot构造计划](../src/quant/moe_residency.cpp#L1377)，入口
[ParseResidencyConfig](../include/q4t/runtime/residency_config.h#L57)。

接口声明忽略重复专家，但InitHot构造计划时只查已经提交的
expert_slot_；本次计划尚未提交，因此列表中的重复项都会占用槽。
配置解析也接受重复项，并按原始列表长度推导槽数。

使用2专家/2槽及合法配置`{"0":[0,0,1]}`，解析和InitHot均成功，
两个槽却都记录专家0。随后请求不同专家集合[0,1]，Resolve报告
“needed set exceeds dynamic capacity”，虽然不同专家数恰好等于
容量。该探针使用受支持的inline=2配置以固定提交顺序；不依赖线程
竞争才能触发。建议在计划阶段去重或明确拒绝重复输入，并统一
配置容量、热填充和保护槽的计数语义。

### R3 · P2 · 加载工作线程中的host异常未转为任务失败

位置：[LoadWorkerLoop调用StageTask](../src/quant/moe_residency.cpp#L1651)。

本轮Init回滚已覆盖初始化期分配和线程创建异常，但运行期工作线程
直接调用StageTask，没有异常边界。StageTask的vector分配、字符串
和分片元数据分配均可能抛出bad_alloc；异常逃出std::thread后触发
terminate，调用线程及HTTP入口的catch无法处理另一线程的异常。

在生产Resolve派发到工作线程后，只令该线程的第一次operator new
抛bad_alloc，修复前后都以SIGABRT（returncode=-6）退出，未返回
Status，也没有执行正常任务收尾。测试在独立CPU进程中进行并关闭
core dump；不是主机真实OOM，也不声称复现了GPU错误。

建议在工作线程边界捕获异常，并保证错误传播、done通知和已获取
claim的收尾；涉及已提交DMA时仍须遵守已知完成/保留payload合同。
只补catch而不处理任务完成与claim生命周期不足以闭合该问题。

### R4 · P2 · 监督器只等待主进程，可能把遗留worker误报为已停止

位置：[supervise的finally回收](../tools/release/release.py#L298)。

退出时先向所有进程组发SIGTERM，但后续只等待组长Popen对象；只有
组长wait超时才对该组发SIGKILL。若组长先退出，忽略/未完成TERM的
worker仍可存活；reader的join有界返回后，监督器照样删除状态文件
并记录stopped，调用方随后可能启动新版本。

用真实CPU子进程替换GPU/nginx命令，保留生产的启动session、捕获
线程、killpg、wait和finally逻辑。组长正常被回收、supervisor.json
消失后，同组且忽略TERM的孙进程仍存活；修复前后结果一致。探针
随后通过自有pidfd杀死并作为subreaper回收该孙进程，未留下进程。
此复现针对独立监督模式；外部systemd control-group回收不替代
工具自己宣称的停止完成。

建议以整个受管组/可证明归属的后代为空作为停止完成条件；即使
组长已退出，宽限期后仍应处理残留worker，最后再清除状态文件。

## 原8项修复的回归复核

| 范围 | 本轮检查与结论 |
|---|---|
| A1/A3 | 复查整数、extent、溢出与fd/map RAII；6项Safetensors合同通过。R1是上层消费者的目标容量遗漏 |
| A2 | 复查weight/SF/标量同分片及连续专家分组合并；CPU完整payload比较覆盖同分片与三种拆分 |
| A4 | 复查部分Init、先注册worker所有权、线程失败join和DMA失败保留；CPU注入/重试合同通过。R3位于运行期工作线程 |
| A5 | 复查server准入、共享默认2048、预算自动容量及模型forward行数；复用未变身份的预算/host验收 |
| A6 | 复查prefill/plain/MTP完成边界计数与监督消费者；复用5项监督合同和真实在途HTTP证据。R4是既有停止路径遗漏 |
| A7 | 独立IO入口重新构建零警告；4/4 CTest、24项合同通过 |
| A8 | 复查pidfd实际能力探测先于监控/子进程与CMake解释器选择；12项冻结工具身份/CPU封条核验通过，复用原能力拒绝合同 |

本轮新增：24项CPU合同、修复前后各4个驻留探针进程、修复前后各1个
真实CPU监督器夹具。三次CPU构建零警告。探针中的PASS表示**缺陷被
观察到**，不是修复验收通过；SIGABRT是R3的预期观测结果。

169项运行时文件、默认b11ed66c/982c888c、最终工具封条及只读回退
核验通过。原40HTTP/20模型启动、15真实GPU合同和公共CTest 22/22
按相同身份复用；本轮新增模型加载、GPU、生成HTTP和profile均为0。
没有重新部署默认程序，原回退包、验证证据和失败现场继续保留。
上述检查不构成全面无缺陷证明、整模型新增质量验收或性能结论。

## 证据与下一步

[37份原始证据、构建命令及探针](evidence/main-reaudit-20261031/README.md)
记录当前/父源码哈希、完整输出、退出码和进程清理；本地构建与
父源码快照保留在`.q4t-work/main-reaudit-20261031/`。

建议下一阶段先成组处理R1～R3的读取、热专家计划和工作线程失败
合同，再独立闭合R4进程回收。未改运行时代码，不将审计归档当作
缺陷修复完成；后续修复仍须按实际影响验证，并保留已通过且身份
未变的证据。
