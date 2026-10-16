# HC up TTFT差异定位（2026-10-16）

**追加证据修正**：同一工作区另一会话的`00af64c`揭示两次并发重叠。
下文trace的秒级阻塞不得仅归因于Nsight；它受到已确认的并发加载污染。
轻量诊断启动阶段也曾重叠，HTTP前29.558秒已退出竞争服务，但环境残留
未量化。原始数据保留，阶段3仅作局部观测，根因仍未闭合。

用户授权继续定位，不重复采样挑选结果。此前NO_GO及三对差值保留。
生产代码/main及数值合同不变。此阶段是已有失败定位，不是新性能候选。

## 阶段1：静态核对与诊断计划（执行前冻结）

- A=be74946d…原main，B=f01a0ee6…原HC up，使用保存二进制。
- 原22份CUDA ELF相同，新增HC内核不在200K off形状分派内。
- host按名称折叠后的1046个公共符号地址全部改变，其中只有Bf16SmallMGemm大小变化
  （1040→1176字节），新增HC wrapper/stub及链接器thunk。
  大小一致不等于指令一致，地址/布局仍是待排查因素，不视为根因。
- 固定A、B各一条200K off真实HTTP CUDA trace（共2条），模型加载后
  才开始采集；同原prompt、256输出及容量，不追加TTFT重复测量。
- 比较prefill kernel序列/配置/次数，GPU时间与空隙、拷贝及CUDA API；
  同步只读记录GPU/CPU频率、温度。trace本身会扰动耗时，不能替代
  原E2E或据此撤销失败。若机制未能定位，明确未决，不声称查清根因。
- 诊断工具修正档位长度与实际输入混淆：204800档实际204789，严格
  对照归档responses输入身份；trace分析同样按实际输入核验embedding。
  增加显式诊断标签，不改变任何模型算法、精度或验收阈值。

原始证据目录：.q4t-work/hc-up-cause-20261016/。

## 阶段2：trace结果与下一步定向诊断

A/B各一条均输出一致、服务和export退出0。两侧prefill共2264679次
kernel，四条stream内完整名称/启动配置序列hash全部一致，新HC为0次。
全局开始时间序列不同是并行交错，不是各stream执行工作改变。
CUDA API次数亦相同；本次647次频率观测GPU1575MHz、内存4266MHz、
CPU2601MHz不变，GPU温度44.25–73.375°C。不能据此追溯历史温度。

A的GPU busy158.968s、gap95.525s；B为158.370s、1.897s。
A trace出现cudaMallocAsync最长28.131s阻塞，其他多次秒级阻塞；
B无同样现象。此差异远大于原89ms，不能拿它解释历史回退或反判B更快。
trace只证实了执行身份，未证明原TTFT差异的根因。

针对具体trace失真，冻结第二种观测：不使用Nsight，A/B各一条相同
200K off HTTP，开启二进制已有Q4T_LAYER_TIMING；只以LD_PRELOAD记录
cudaMallocAsync原调用前后单调时钟/size/返回码，不替换返回值、不插入
CUDA同步、不改变计算。日志开销及event同步仍存在，仅用于诊断。
固定2条，不追加同类样本，不改验收规则。对比25块事件时间、分配阻塞
及HTTP边界；保留失真trace，不能将两个观测阶段混合成性能统计。

## 阶段3：低扰动定位结果（不是根因闭合）

固定A/B两条HTTP均通过原输入/输出合同、服务退出0，6602次分配均返回0。
探针零警告构建，生产binary hash不变；没有新增候选或修改数值阈值。

| 观测 | main A | HC up B | B-A |
|---|---:|---:|---:|
| HTTP TTFT（ms） | 158883.374 | 158971.292 | +87.918 |
| 25块层事件区间之和（ms） | 158684.43 | 158770.50 | +86.07 |
| 线性注意力层组（ms） | 82484.10 | 82600.45 | +116.35 |
| 全注意力层组（ms） | 76195.26 | 76164.98 | -30.28 |
| head（ms） | 5.01 | 5.01 | 0 |
| 分配API累计wall time（ms） | 6575.182 | 6594.690 | +19.508 |

层事件区间包含host提交造成的GPU等待，不是纯GPU busy；线性注意力
层组还包含该层的投影、HC和MoE，不能把+116ms直接归为GDN算子。
分配API与事件区间重叠，不能将两项相加。约97.90%的本次HTTP差值位于
层事件区间内，区间外剩+1.848ms；不支持将本次差异主要解释成HTTP
解析、首token发送、层外scheduler或head成本。

25块差值有正有负：第11块-15.47ms，第12块+42.52ms，第13块+16.80ms，
其余各块差值绝对值均小于9ms。第12块分配差仅+0.272ms，第13块为
-0.869ms，且两侧所有分配大小/顺序完全相同。无Nsight时没有秒级
分配阻塞，故首次trace异常不能直接解释原始小幅TTFT差异。
这不是零分配开销证明，也不排除分配与调度的间接相互作用。

## 构建与环境排查边界

两份原始CMakeCache所有CMAKE_配置相同。旧脚本按名称折叠后的1046个公共host符号中，
按符号精确范围反汇编，有11个mnemonic序列不同，包括小M分派及部分
地址相关链接改写。实际g++-14 specs默认传--fix-cortex-a53-843419，
二进制存在e843419跳板；例如LinearAttentionForward末部原load处的一方
变为跳转到链接器生成代码。此证实host不能按“GPU ELF一致”视为逐位
相同，但尚未测出这些跳板对端到端差异的贡献。未通过随意改链接参数
制造新的有利布局来宣称修复。

内核系统日志显示原复核和本次时段均有周期性I2C超时。没有独立证据
证明它与候选相关，故仅保留环境线索，不作根因。原始六次复核缺少同期
层/API/频温数据，不能将本次诊断追溯套用为历史三对的直接归因。

## 当前结论与未闭合项

已查清：新HC内核没有执行；没有增加prefill工作、改变cuBLAS kernel
选择或启动配置；当前观测无频率降档；低扰动差异主要位于已有prefill
层执行区间，且不能由单一分配阻塞解释。尚未查清：host地址布局/
提交时序、多stream交错及物理运行条件分别贡献多少，不能宣称某一项
是89ms的根因，更不能据此直接称原回退为噪声。

本轮止于可审查的定位边界：原NO_GO保留，未做无依据的产品修复，
未重跑已通过五档/数值测试。未补候选缺失数值验收，main仍不变。
若继续因果验证，需要保持其余身份的host布局/提交时序对照，并观测
线性注意力层组内部等待；单纯增加相同TTFT样本或再换布局挑快者不能
闭合因果关系。该对照尚未实施，不将建议写成已完成。

[可审查证据](evidence/hc-up-cause-20261016/README.md)保留脚本、身份、
逐条结果、stream hash、分块分配记录及大型原始工件manifest。

## 后续到达的并发证据（优先于前述阶段解释）

最终推送时发现另一会话已提交[排程记录](GDN_GPU_QUEUE_2026-10-16.md)
（00af64c）。据其原始environment-abort和events，与Nsight session原点
对齐（UTC）：GDN服务03:43:43.884–03:45:30.519存活，HC A trace最大的
三段无本进程GPU活动区间分别为03:43:56.728–03:44:43.606、
03:44:44.226–03:45:17.194、03:45:18.694–03:45:30.290，完全在重叠内。
这证明A trace不是隔离对照，并发加载是具体混杂因素；不能只归因于
Nsight，也未量化它与跟踪各自贡献。这里的gap是目标进程的活动空隙，
**不是整块GPU空闲**，外部进程活动不在该trace计数中。

轻量A服务03:58:01.750启动，GDN服务03:58:03.454确认退出；HC HTTP
03:58:33.012才发出，相隔29.558秒。未发现该已知竞争服务与轻量HTTP
区间直接重叠，但此前启动资源争用的残留影响没有被隔离，因此不能
称这对观测完全排除了环境混杂。它仍可保存输入输出和分块时间事实，
不能独立证明binary布局导致原89ms差异。

我方此前只保证本会话内部串行，未建立跨会话全程互斥，属于诊断排程
缺口。后续任何因果对照都须先取得跨会话独占安排，并全程检查竞争
进程；不能仅凭启动前空闲快照。此次不补跑或挑样本，保留全部污染
证据；原NO_GO不变，本会话不再启动GPU任务，另一会话可按其排程继续。
详见证据目录environment/alignment.json及双方原始事件。

## 活动目标与完成标准

2026-10-16用户明确要求设置“查清原因并妥善解决”的目标，已激活。
此前诊断阶段收口不代表此目标完成，原NO_GO继续保留。

1. 建立跨会话GPU独占与全程竞争进程检查，记录环境及运行身份。
   已知并发污染数据只用于其能支持的事实，不充作隔离性能对照。
2. 执行前冻结假设、受控变量、诊断次数和判据，用有区分力的对照
   建立因果证据。不重复TTFT采样碰结果，不随意更换布局挑快者。
3. 对确认的原因修复实现或评估/排程流程，并形成可回退阶段提交。
   保持N=4合同、确定性、HTTP合同及普通decode性能，不扩展到无关
   prefill优化。不能将环境解释当作未经验收的性能实现通行证。
4. 只重验受影响或缺失项，复用身份未变的已通过证据。接受性能实现
   仍须有效五档E2E及相关数值/质量合同。明确给出HC up可合入或有
   因果依据的保留/废弃结论，不自动合并main。

每阶段更新STATUS与当天追加日志，提交推送现有工作分支，保护其他
会话成果。未查清、仅缩小位置、文档提交或CI通过均不构成目标完成。

## 活动目标阶段1：共享互斥入口落地

2026-10-16重新检查：GDN研究已收尾并释放窗口，未检测到运行中的
模型服务/Nsight。新增gpu_exclusive.py，按git common-dir找到仓库级
flock，持锁覆盖完整阶段，附带启动前及运行中/proc与NVIDIA计算进程
监控；冲突/查询失败返回ENV_ABORT，仅清理自有进程组。
AGENTS与EVALUATION已要求后续开发GPU验证统一使用该入口。

8项CPU合同通过：锁冲突不启动、不改写持有者；启动前冲突；查询错误
不当作空闲；自有进程组与未知计算进程识别；命令字符串不误判；
成功释放；运行中冲突只终止自身；命令失败与环境失败区分。
本机guard包裹/bin/true的无推理检查通过，实际/proc及NVIDIA查询正常。
已注册公共host CTest。没有运行模型、HTTP、GPU数值或新性能采样。

边界：共享锁只约束合作入口，进程轮询并非全硬件排他证明，短暂或
不可见外部负载仍可能漏检；不以监控无冲突代替用户约定的验收。
下一步是冻结能区分版本效应、运行顺序和测量波动的因果对照，包含
同身份负控，明确其能证明与不能证明的结论；尚未启动新推理对照。
目标仍active，根因与HC up交付未完成，原NO_GO及污染记录继续保留。

## 因果实验1：只干预链接器跳板，保留地址布局（执行前冻结）

共享互斥入口提交895fa2d的公共host CI已通过。当前无其他已检测GPU
任务，后续整个实验持同一把锁，模型加载、间隙与最后清理均覆盖。

假设：A/B不同的e843419 load跳板额外执行成本，是TTFT差异的来源。
它与“代码地址/缓存布局本身”是不同假设，不能通过重新链接混在一起。
静态核实本机CPU part=0xd83；Linux主线cputype.h将其标识为Neoverse
V3AE，Cortex-A53为0xd03（[原始定义](https://raw.githubusercontent.com/torvalds/linux/master/arch/arm64/include/asm/cputype.h)）。
这是本机有界诊断，不将移除A53 workaround泛化到其他CPU或产品构建。

准备四个诊断文件，均不修改原始binary或部署：A0/B0为原main/HC
逐字节复制；A1/B1只改6/5条4字节指令。脚本逐一验证原入口B确实
跳到只含LDR与B返回的8字节跳板、该入口唯一、返回为原位置+4；
将入口B替换为该LDR，不修改dead thunk、padding、地址、符号或CUDA
内容。全部非.text段逐字节比较通过，原运算与数据访问不变。
这些派生文件保留原build-id但有独立SHA/derivation记录，绝非release。

固定顺序A0 B1 B0 A1 / A1 B0 B1 A0，四身份各两次新鲜服务，每次仅
原200K off请求，实际204789输入、256输出；启用已有Q4T_LAYER_TIMING
作辅助分组，不使用Nsight或LD_PRELOAD。无额外预热/连接探测。
所有请求沿用既有输入/输出/容量检查。单一guard持锁；5秒只读频率、
温度、内存与压力采样，记录采样时刻及错误，无法视为全硬件确定性。

判据在protocol.json冻结：同身份两次的最大绝对差定义为R；每组计算
原差g0=B0-A0、干预后差g1=B1-A1，以及交互差g0-g1。
只有两组同时g0>0、交互差>R、abs(g1)<=R，才支持可辨识的跳板贡献。
该保守诊断条件不是显著性检验或验收门槛；未满足只说明该解释尚无
足够支持，不能证明零效应/噪声/等价，更不解除原NO_GO。
同身份负控用于约束干预解释，不能当作追加正式基线的样本。

任何竞争、监控错误或HTTP合同失败停止保留现场，不自动重跑；完整8条
后不扩样。只检验已失败档与具体链接改写，不重跑五档已通过项目。
[协议与派生证据](evidence/hc-up-thunk-cause-20261016/README.md)。


## 因果实验1结果与下一机制（2026-10-16）

固定8条已全部完成，无扩样。8个服务退出0，输入/输出合同全部通过，
同一输出SHA为57f3581d…；guard全程持锁，1603次运行轮询未检测到
竞争，退出COMPLETED。此结论限于轮询可见范围，不代表硬件绝对隔离。
333条环境快照中/proc/pressure/{cpu,memory,io}始终不存在，共999个
读取错误已保留；不能据此排除系统压力。其他频温范围见completion-audit。
这不是guard查询失败；协议要求记录辅助遥测错误，未因此追加实验。

| 固定组 | 原B0−A0(ms) | 干预后B1−A1(ms) | 交互差(ms) | 冻结判据 |
|---|---:|---:|---:|---|
| 1 | 136.307 | −22.335 | 158.642 | 未满足 |
| 2 | 272.753 | 124.238 | 148.515 | 未满足 |

同身份负控最大差R=190.534ms（B1），两组交互差均未超过R。
B0−B1分别+103.106/−17.427ms，方向反转；A0−A1均为负。
因此本轮不能将跳板执行成本认定为可辨识根因。不能反过来证明零效应、
原回归是噪声或候选等价；不修改判据、不追加请求，原NO_GO继续有效。
所有诊断副本禁止部署，未替代五档、质量或MTP数值验收。

静态审计更正：旧check_host.py按符号名建字典，折叠了同名局部函数。
“1046公共函数”实际是折叠后的公共名称，不能当作全部函数穷尽比较；
新增CUDA注册构造函数尤其不能忽略。完整nm行数A/B为1149/1153，
不同名称1052/1054，重复名称清单已归档。该限制不改变CUDA ELF逐字节
比较或跳板干预的指令校验，但撤回其对全部host代码差异的覆盖含义。

候选9020处构造函数的init-array相对重定位为3ef098→9020；函数读取
136d78处HC kernel名称，注册host stub地址64bac，调用
__cudaRegisterFatBinary、__cudaRegisterFunction及FatBinaryEnd，并登记
退出清理。由此确认即使HC内核未调用，该新增模块仍在进程启动注册。
这是待验证机制，不是TTFT归因：注册自身发生在HTTP之前，但其产生的
运行时注册表/模块状态可能影响后续执行。下一步拟只禁用该构造函数，
保持候选地址布局不变，限定off诊断；先冻结身份、对照、测量与判据。
不直接以重新链接或重复原TTFT采样替代机制检验。

[完整结果、退出审计、重复符号与注册反汇编](evidence/hc-up-thunk-cause-20261016/README.md)。


## 因果实验2冻结：仅取消新增 HC 模块注册

原 A/B 不变，C 从 B 原位仅替换9020处入口为RET，不重链接。校验原SHA、
init-array项、注册名称与host stub指令，所有非.text内容与相对地址不变。
C 的SHA为72388969…，仅用于off；因HC未注册，绝不能执行MTP或部署。

固定 A B C / C B A 六个新鲜服务，各一条原200K off输入与256输出。
全阶段同一GPU lease，事件诊断与上轮相同，无额外请求或自动重试。
同身份差最大值定义R；两组均需原差B−A>0、注册效应B−C>R、
残差abs(C−A)<=R，才支持可辨识的注册状态贡献。否则继续未决，不能
归为噪声或解除NO_GO；即使支持，也不能直接指定CUDA内部实现根因。
注册发生在HTTP前，此干预检验其留下的运行时状态，不把启动耗时计入
TTFT。ASLR/物理映射未固定，系统pressure接口缺失，边界明确保留。
[协议、派生身份与脚本](evidence/hc-up-registration-cause-20261016/README.md)。


## 实验1事件区间离线补核

不增加请求，解析8个既有server.log：每条恰为24×8192+8181的25段。
原B−A两组HTTP差136.307/272.753ms，事件总区间差135.880/270.510ms，
外围残差仅0.427/2.243ms。同身份B1第二次−第一次HTTP差190.534ms，
事件差190.190ms，其中lin层组181.660ms。说明本轮波动本身也主要位于
层事件区间，不能用“差在lin组”证明GDN或HC计算根因。
model.cu的FinishLayerTiming按整层相邻事件聚合，包含host提交饥饿、
依赖等待与整层其他算子，不是attention kernel净运行时间；打印精度
为0.01ms，合计有舍入误差。结果及解析脚本已归档，不改变冻结判据。


## 实验2结果：注册解释不成立于本轮判据，修复监控退出竞态

六个HTTP请求均完成、输出SHA一致、服务退出0。原guard在最后收尾
报告ENV_ABORT，必须保留，不能把它改写为COMPLETED。唯一报告PID
1149505就是06-A/server.pid；client_after=355738.616869，冲突报告
355738.995155，server_after_shutdown=355739.086750（monotonic秒）。
报告无/proc字段，与NVIDIA查询之后自有服务退出、/proc缺失的竞态
一致，不是外部竞争的证据。父runner在收尾被SIGINT中断，未写第六条
聚合；从六个已完成results/exit只读重建到独立recovered文件，保留原件。

| 组 | 原B−A(ms) | 注册效应B−C(ms) | 残差C−A(ms) | 冻结数值判据 |
|---|---:|---:|---:|---|
| 1 | 54.443 | −78.869 | 133.312 | 未满足 |
| 2 | 320.280 | 52.049 | 268.231 | 未满足 |

R=204.820ms（B自身两次差）。效应方向反转且小于R，即使独立于guard
失败查看六条完成请求，也不支持将注册状态认定为根因。原阶段仍标记
ENV_ABORT，不以离线重建代替完整阶段正常退出，不追加或重跑请求。

修复gpu_exclusive：记录已见自有进程身份；仅当NVIDIA返回历史自有PID
而/proc已缺失时，重新查询两者，只有两边均确认消失才记录retired事件。
活跃复用PID、不可读PID、未知PID及持续存在的context仍失败关闭，查询
错误不视为空闲。新增4项针对退出、复用/新context、未知/不可读、复查
错误的CPU测试，合计12项通过；专用host CTest通过。错误地在build和
build/public-host查找CTest时无测试，不计为通过；实际通过目录为
.q4t-work/hc-up-isolation-20261016/host-cmake。
该修复解决监控收尾缺陷，不宣称解决原TTFT回归。

追加静态边界：Bf16SmallMGemm的M范围检查在64728–64730，M=1/8181/
8192跳到返回路径；新增HC判断从64788才开始，因此该档不执行新增分派
比较。这不等于整个host函数相同，前后序、相对地址与CPU缓存仍可能不同。
后续应补能区分host提交停顿与GPU工作/等待的低扰动观测；两次干预均
未形成根因证据，不再重复相同TTFT实验或跳到候选验收。目标继续active。


## Host区间观测准备（CPU校验，尚未启动模型对照）

新增host_interval_probe.cpp，用LD_PRELOAD拦截既有T>1层计时报告，
保留原格式/参数，按线程记录单调时钟、线程CPU、schedstat、切换/缺页、
user-only PMU cycles/instructions/branch misses及enabled/running。
不拦截每个CUDA launch、不调用CUDA、不重编译模型。第一报告建立基线，
只分析后续相邻报告；不能覆盖第一chunk或代表GPU净执行时间。

本机双线程CPU夹具确认忙100ms与睡眠100ms可区分、线程计数独立、T=1
不采集、原输出逐字节不变，三PMU事件可用，零警告编译。发现首次PMU
初始化34.874ms，已独立记录并把基线移至其后；后续捕获数十微秒但日志
写出尚不含在capture内。插桩总TTFT不能用于性能接受，也不能无证据
宣称低扰动到足以解释原0.1秒差异。PMU排除kernel，线程CPU包含kernel，
解释时必须区分；工具观察当前线程，不能直接证明GPU busy或其他线程等待。
下一步先冻结观测范围与有效性门槛，再进行必要机理对照；尚无新增HTTP。
[CPU夹具、身份与结果](evidence/hc-up-host-observer-20261016/README.md)。


## Host观测协议冻结

固定A B B A四条200K off，新鲜服务原输入/256输出、原binary，无额外
请求。全阶段持同一GPU lease，Q4T_LAYER_TIMING与host probe保持相同。
有效性要求单线程25报告/原chunk形状，错误码为0，计数单调，每区间
PMU running/enabled>=99%，单次capture<=1ms且累计<=5ms。初始化与
日志写出另论，插桩TTFT禁止验收。第一chunk排除，仅分析2–25区间。
每个指标独立报告两对B−A与同身份R，两对均正且>R只支持版本相关观测，
不是显著性检验或因果证明。CPU时间可含CUDA忙等，PMU仅user态，不能
将CPU增加直接判为提交变慢，也不能把剩余时间判作GPU净耗时。
如观测无效不扩样；如host指标可辨识，下一步归因执行/自旋；如仅事件
指标可辨识则进一步区分设备/driver依赖。无可辨识差异则保留未决，
不把噪声当根因。不重复跳板或注册干预，原NO_GO与生产main不变。


## Host区间观测结果（2026-10-17）

四条全部完成，输出合同通过，guard退出COMPLETED，803次运行监控无
检测到的竞争。每请求25条同线程报告、计数单调、无读取错误，PMU覆盖
100%；捕获累计1.315–1.374ms，初始化21.438–29.848ms另列。冻结有效性
检查通过。此次未触发retired-owned分支，不能据此宣称真实退出竞态已经
由GPU运行覆盖；该分支的验证仍是此前CPU合同。

| chunk2–25指标 | 第一对B−A | 第二对B−A | 同身份R |
|---|---:|---:|---:|
| 层事件区间(ms) | 303.000 | −131.430 | 261.320 |
| 线程CPU(ms) | −311.727 | −222.368 | 235.622 |
| runnable排队(ms) | 9.131 | −2.779 | 8.184 |
| user指令数 | 18190887 | 5788343 | 16215008 |
| branch misses | 397587 | −780599 | 643737 |

没有任何报告指标满足两组正且都超过各自R的冻结条件。两组候选CPU
时间均少而事件区间方向反转，不支持本轮观测到稳定的host额外工作/
排队解释；不能推论原历史回归纯属噪声、零效应或候选可接受。
观测线程CPU约16.94–17.40秒，wall约153.33–153.64秒；差值不能直接
叫GPU busy，因为包括阻塞、依赖与未观察线程，CPU本身还可含CUDA自旋。

不追加相同观测、不重跑数值/五档。下一步先复用已有device trace进行
离线依赖/空隙审计，分清已证实的污染区间及可用范围，避免将全部A/B
trace总量直接归因；只有明确测量缺口后再冻结必要定向设备观测。
[全部结果与完整退出审计](evidence/hc-up-host-observer-20261016/README.md)。


## 旧device trace离线审计（2026-10-17）

按25次EmbedLookupKernel对齐chunk，用kernel/copy/memset区间并集
和补集分解跨度，逐chunk验证恒等式，不新增推理。已知竞争加载结束
为A相对99.135秒，chunk2起点103.947秒，直接时间重叠集中于第一chunk；
后续残留效应未量化，不能称隔离对照。

| chunk2–25（秒） | A/main | B/HC | A−B |
|---|---:|---:|---:|
| 起点到首logits D2H截止跨度 | 155.592796 | 154.957239 | 0.635558 |
| 该进程设备活动区间并集 | 153.730933 | 153.236428 | 0.494505 |
| 区间空隙 | 1.861863 | 1.720811 | 0.141053 |

旧trace中A更慢，与原HC小回归相反，不能直接归因历史结果。A首chunk
空隙93.662秒，B0.175秒；必须保留原污染记录，不用切片掩盖污染。
活动并集不是整卡busy，空隙也不一定整卡idle；跨stream kernel时长之和
会重复计数。next关联API尚未进入的时间只说明时序，不能单独叫CPU慢。

进一步核对chunk2/13/25的最大空隙：结束于20,971,520字节（8192×2560）
或20,943,360字节（8181×2560）的H2D，其后是Fp8ToBf16Kernel；之前API
为短cudaEventSynchronize。源码GatherRows在upload_complete同步后调用
reader->Gather，再H2D并转换，吻合PLE staging路径。B例空隙约11.575/
12.321/12.349ms，A对应56.546/11.406/21.289ms。此确认PLE读取/整理可以
产生所见空隙，但没有区分SSD等待、CPU scatter、缓存与内存压力，也不
解释稳定HC版本回归。下一步限定Gather阶段观测，补齐CPU/等待分界；
不删除同步或修改数值，不启动无关PLE/prefill优化，不重复原TTFT采样。
[完整离线脚本、区间与拷贝证据](evidence/hc-up-device-audit-20261017/README.md)。


## Gather观测边界准备（2026-10-17，仅CPU夹具）

PageReader已有io/scatter/total统计但未对外输出；重新链接加日志会改变
本次关注的host布局，故选择原动态API边界。新gather_interval_probe转发
cudaEventSynchronize、目标H2D与io_uring_submit_and_wait，记录成功同步
返回到目标拷贝入口的线程CPU/墙钟，以及其中uring API的CPU/墙钟。
这覆盖reader->Gather附近区间，不含之前ComputeRowIds或同步等待，
不能称整个Gather。原trace目标拷贝为24×20,971,520+1×20,943,360，
每chunk一次；实际新观测仍须验证形状、线程、顺序与错误码。

CPU假库夹具验证20ms同步被排除，20msCPU与30ms假uring等待分开；
两线程、无同步、同步失败、I/O错误、普通形状过滤和原参数/返回/errno
均检查通过。零警告编译；格式化后二进制SHA不变，复用已通过证据。
未调用真实CUDA/uring或运行新HTTP。PageReader只读普通文件，page cache
可能服务读取；uring API时间并不等于SSD时延，剩余CPU区间不是纯scatter。
下一步冻结有限真实边界观测，保持生产main与数值/同步语义不变。
[工具身份与CPU校验](evidence/hc-up-gather-observer-20261017/README.md)。


## Gather真实观测冻结（2026-10-17）

固定A B B A四条原200K off，新鲜服务，同一GPU lease，layer timing+
Gather probe，不叠加PMU/Nsight。用HTTP事件单调时钟窗口筛选并记录
窗口外加载拷贝；必须一一对应25次T×2560上传与25个chunk报告。
第一chunk无条件排除，后24条要求成功同步、I/O/拷贝无错误、提交等待
次数与requested_completions跨身份逐chunk相同；检查区间包含关系与
CPU端点偏差。分析各区间CPU/wall、uring API CPU/wall及剩余区间，
每指标报告同身份R与两组B−A；两组均正且>R只是可辨识观测，不是因果。
比较Gather变化与层事件变化的方向/量级，但不假定Gather全部位于GPU
关键路径，不把API等待当SSD时延，不拿插桩TTFT接受候选。
失败停止留证，负结果停止该测量类别，不扩样凑结果。原NO_GO保持。


## Gather观测结果（2026-10-17）：本轮事件差不能由Gather解释

固定四条全部完成，HTTP合同通过，guard正常退出，804次轮询未检测
竞争。每条25次上传与chunk报告一一匹配，后24条I/O工作量完全相同，
同步/I/O/拷贝无错误，冻结有效性通过。没有新增或筛掉请求。

| chunk2–25指标（ms） | 第一对B−A | 第二对B−A | 同身份R |
|---|---:|---:|---:|
| 层事件总区间 | 122.850 | 100.690 | 41.990 |
| Gather边界wall | −6.271 | 2.059 | 6.546 |
| Gather边界CPU | −5.741 | 1.820 | 6.489 |
| uring API wall | −6.752 | 3.114 | 7.959 |
| 其余wall | 0.481 | −1.055 | 1.413 |

本轮层事件差满足冻结的可辨识观测条件，但Gather不满足，且差值方向/
量级均不能解释。Gather合计wall574.193–580.739ms，CPU572.888–579.377ms；
uring API wall308.483–316.442ms，CPU307.179–315.021ms，主要不是本线程
阻塞等待。普通文件page cache/内核处理仍不能等同纯SSD服务时延。
按冻结规则停止Gather测量类别，不加样本、不改读取或同步。

既有日志离线分组：full层组两对差+108.440/+124.210ms，lin组为
+15.060/−23.780ms。本轮偏向full组，与早先定位到lin组不同，不能合并
成稳定单算子根因。full组包含MoE和HC等，尚未定位到attention本身。
已有LayerSubTiming支持full_qkv/full_core/full_oproj及其他分项，原
main二进制具备开关；下一步复用它冻结有界细分观测，明确额外events
和同步的扰动，不重新链接模型、不扩展为prefill优化。目标仍active。
[完整结果及退出审计](evidence/hc-up-gather-observer-20261017/README.md)。


## 细分事件观测冻结（2026-10-17）

原binary具备Q4T_LAYER_SUBTIMING，无需重新链接。相关历史目录未找到
200K细分日志可复用。固定A B B A四条原200K off，原输入/256输出，单
GPU lease；只开layer与subtiming，不叠加其他preload/PMU/Nsight。
保留所有25chunk，比较2–25，四类报告逐条检查形状、字段、有限非负、
同步错误和顶层求和。每指标两对B−A均需>max(同身份R,1ms)才作为
可辨识正差；1ms覆盖打印舍入，不构成因果或接受门槛。

源码边界：full_qkv=QKV投影，full_core包含QSA indexer/sparse attention
及相关操作，full_oproj为输出投影；MoE/HC细节跨所有层，不能隔离full
层的MoE/HC。PLE top slot在Gather之后。嵌套detail与顶层不能重复相加。
额外CUDA events和同步影响调度，不用插桩TTFT验收，不把区间等同GPU
kernel净时间；负结果停止此类别，不扩样。目标及原NO_GO保持。
[协议和已冻结分析脚本](evidence/hc-up-subtiming-20261017/README.md)。


## 细分协议覆盖错误与主动中止（2026-10-17）

首条运行发现预期四类报告中的moe-subtiming始终缺失。源码复核：其
Start/Stop只在moe_decode.cu的小T decode路径，大T prefill不走该分支。
此前对覆盖范围的判断有误。顶层sub-timing仍包含完整moe区间，但不能
分成内部prep/gemm/combine。不是模型数值失败，不得伪造缺失计时。

主动终止自有guard，状态ENV_ABORT（guard interrupted），清理完成，
GPU查询为空；首条无完整exit合同记录，零条完整通过，不把部分计时
用于A/B结论。剩余三条未运行，原协议/中止/日志SHA均保留。
修订解析器只要求实际支持的三家族，保留形状/字段/同步错误/顶层求和
要求，5项CPU回归通过；没有根据耗时结果更改差值门槛，也没有新推理。
下一版先冻结修订协议，并每个HTTP结束就校验覆盖，再继续下一条。
这次修复针对诊断夹具错误，不等于TTFT回归根因闭合。原NO_GO保持。
[中止证据](evidence/hc-up-subtiming-20261017/coverage-failure.json)、
[修订解析器与CPU检查](evidence/hc-up-subtiming-20261017-v2/README.md)。


## 修订版细分协议冻结（2026-10-17）

v2只修复已确认的prefill报告覆盖错误：要求layer/sub/HC三家族，保留
顶层moe，排除没有实现的prefill MoE内部细项。原A B B A四条、binary、
输入输出、形状/字段/求和及max(R,1ms)差值规则不变。每个HTTP结束即
校验报告覆盖，通过后才进行下一条；错误退出，不自动重试。
v1首条没有完整合同结果可复用，原现场/协议/ENV_ABORT不改写。本次只
重验夹具缺陷影响的细分观察，不重复其他已经完成的观测或五档数值测试。
额外event/同步扰动边界保持，不将插桩TTFT用于验收，不宣称根因完成。


## v2自有退出误报再次暴露进程扫描缺陷（2026-10-17）

01-A/02-B均完成、输出合同通过、服务退出0。guard在02-B清理时报告
PID1169273，正是该服务；case_completed=360200.438522，报告=
360200.837163，server_after_shutdown=360200.909142（monotonic秒）。
原ENV_ABORT保留，03-B/04-A未执行。02-B三家族覆盖已离线验证；两条
完整证据锁定保留，不能据部分AB给出完整反向对照结论，也不重跑它们。

发现processes的stat/cmdline/exe共用异常处理：退出时exe链接可能先
消失，却把已读到的pgid/start_ticks一同丢弃。修改为先保存stat身份，
再单独读取可选metadata；未知/非自有compute PID仍冲突，不作PID历史
无条件豁免。CPU真实zombie夹具复现exe缺失但stat存在，归属检查通过；
模拟非自有同状态仍冲突。合计14项CPU合同和专用host CTest通过。
清理时zombie已退出，不再把等待父进程回收的条目当作活跃GPU子进程。
历史冲突没有保存当时stat，不能声称已证明该PID当时处于Z状态；已
证明的是扫描缺陷可复现，以及现场自有退出被误报。监控修复不等于
原TTFT根因解决。下一步先冻结只补03/04的证据复用与中断边界，不能
伪称原lease一直连续，不新增额外样本或重跑已通过身份。

## Subtiming v2分段恢复冻结（2026-10-17）

已核对01-A/02-B原始结果、日志与退出记录SHA；仅补03-B、04-A各一次，
新lease-resume覆盖两条完整驱动。禁止覆盖原lease、环境、遥测、结果，
驱动遇到已存在目的目录即停止。原两条覆盖及输出合同直接复用。
中间监控空档/环境残留未知，四条只能作分段描述；保留max(R,1ms)算术
但full_protocol_passed=false，不作完整连续ABBA、因果或性能验收结论。
完成或失败后停止该观测家族，不追加样本。生产与数值合同无修改。
冻结协议与恢复脚本见evidence/hc-up-subtiming-20261017-v2/resume-protocol.json。
下一步执行两条并离线检查子操作区间；活动目标和原NO_GO保持。

## Subtiming分段结果：停止该观测家族（2026-10-17）

- 03-B/04-A各一次完成，输出与固定基线一致，三家族25chunk覆盖通过，
  服务退出0，恢复guard COMPLETED。原01/02直接复用，原ENV_ABORT保留。
- chunks2–25两对B−A：总层事件+55.350/−50.150ms（R174.420）；
  full_core −67.920/+32.840ms（R73.110），full_qkv +14.360/−9.030；
  MoE +74.770/−19.000；HC up −9.160/−12.470ms。
  未支持固定在full core或HC up的增加，不能将先前full层组差归因该内核。
- 仅PLE顶层区间+2.180/+2.090ms超过算术阈值1ms，量级无法解释历史
  89–247ms，且跨中断对照不具备原连续协议有效性；不据此优化PLE。
- 全部比较保留分段限制，full_protocol_passed=false，非因果证明、非性能
  验收；插桩decode吞吐不作为普通decode回归。停止该观测家族、不扩样。
- 下一步先离线核对环境遥测、测量定义和启动状态的共同变动，选择能区分
  环境与身份效应的因果干预，执行前另冻结；不能将未定位解释为纯噪声。
  本轮未修改生产代码/二进制，main数值证据复用；原NO_GO、目标active。

证据：evidence/hc-up-subtiming-20261017-v2/subtiming-resumed-review.json、
resume-audit.json；原始结果与监控分别保留在原lease和lease-resume。

## 已有环境遥测与TTFT定义离线审计（2026-10-17）

- 范围：host、Gather、subtiming v2共12条完整请求，读取已有5秒系统遥测，
  仅选client_before至case_completed内部采样；含decode，端点未插值。
  所有来源SHA与未覆盖端点时长保存在environment-audit证据，不新增GPU请求。
- 所有采样CPU 2601000kHz，GPU GPC 1575000000Hz，NVD1692000000Hz，
  bwmgr4266000000Hz。各请求采样区间pgscan_direct/kswapd、compact_stall/
  success、pswpout增量为0。不能排除采样间短暂变动、启动阶段影响。
  温度跨阶段范围41.375–77.750°C，不以固定采样频率证明无热/功耗影响。
- pgmajfault增量0–28、pswpin0–42，是系统级计数，不归因模型，也不足以
  解释历史差异；/proc/pressure不存在，不能误记为压力零。缺少进程级
  缺页、GPU实际执行周期与启动后物理布局证据，不能断言环境相同。
- 计时源码：run_acceptance.py读取数据库first_chunk_latency；本地evalscope
  default_api.py在序列化后perf_counter起表，首个非空SSE内容止表。
  chat_generation.cpp:570在首token argmax后记录服务端TTFT，早于内容发送。
  空assistant header不算客户端首token；off不做MTP初始化。两种TTFT边界
  不同但当前没有发现实现错误，不混用。此前层事件差仍不能由网络定义解释。
- 决策：现有证据不支持以稳定降频或请求内内存回收作为已确认根因，
  不启动相关盲目调参。继续优先核对启动/分配地址与实际kernel提交身份，
  明确剩余可干预因素后冻结下一实验；不恢复已停止的观测家族，不改门槛。
  原NO_GO、main生产身份与活动目标保持，根因仍未闭合。

证据：evidence/hc-up-environment-audit-20261017/{audit.py,result.json}。

## Launch资源与地址证据边界复核（2026-10-17）

- 离线扩展旧trace比较至23个launch/resource字段：包含寄存器数、cache配置、
  local/shared memory、cluster配置等；A/B各2264679次prefill kernel的计数
  和四stream内完整序列哈希均相同。stream priority/flag按ID匹配亦相同。
  这补足原先仅比较grid/block/shared大小的范围，不用受污染A耗时归因。
- 旧alloc_probe.cpp只打印size/起止/rc，不记录返回指针、stream或free；
  旧SQLite没有memory表，也不保存kernel参数。6602次分配大小序列相同
  不能证明虚拟地址、池复用关系、实际参数或物理布局相同。
- A为PIE；ASLR与运行时库布局未由旧证据锁定。CUDA ELF相同、上述资源相同
  也不证明跨stream到达时序、GPU代码地址或实际执行周期相同。
- 决策：不再采样已有launch配置或子操作计时。下一项工具须记录成功分配
  返回地址、size、stream、释放关系，并覆盖同步/异步分配与启动阶段；
  首先CPU假CUDA验证转发/错误语义和记录完整性。地址只作诊断，不主动
  改写分配或数值路径；真实请求次数/归一化判据在工具完成后另行冻结。
  若仅地址相同，也不能排除物理布局或host时序，不将此作为因果完成。
- 本轮只读取已有证据，无新GPU请求、无生产变更；main合同继续复用，
  原NO_GO与活动目标保持。下一步实现上述有界地址观察能力。

证据：evidence/hc-up-launch-audit-20261017/{audit.py,result.json}。

## 地址探针工具阶段完成（2026-10-17，未运行GPU）

- 新增allocation_address_probe.cpp，覆盖冻结A/B动态导入的cudaMalloc、
  cudaMallocAsync、cudaMallocHost、cudaHostAlloc、cudaFree、cudaFreeAsync、
  cudaFreeHost。记录进入序号、线程、起止、返回虚拟地址、size、stream、
  flags、rc；无新增CUDA调用、不改分配结果。并发按进入序号/时间分析，
  不能把异步free返回解释为GPU已释放；驱动内部不经这些符号的分配不可见。
- CPU假CUDA验证：7 API、2线程、560条完整记录；参数/返回/errno双向保持，
  失败分配用无效输出地址验证探针不解引用；零警告-Wall/-Wextra/-Werror。
  原始日志、构建命令和SHA已锁定。未加载CUDA驱动，未新增GPU/HTTP请求。
- 适用边界：虚拟地址不等于物理布局；日志扰动时序，任何TTFT不作为性能
  验收。不能仅凭地址不同认定根因，也不能因地址相同排除host或物理因素。
- 下一步冻结仅A/B各一次地址调查：分析启动与请求的分配/free关系、相对
  地址及复用；只为确定后续可控变量，不扩大既有计时样本家族。工具通过
  不代表模型数值/性能通过；生产身份、原NO_GO、活动目标保持。

工具：tools/evalscope/allocation_address_probe.cpp、test_allocation_address_probe.py；
证据：evidence/hc-up-address-observer-20261017/。

## 地址调查执行范围冻结（2026-10-17）

A/B各一次原始200K off HTTP、输出256，只有地址探针preload，无事件/PMU/
Nsight。整阶段共用guard，无预热/自动重试；遇输出/退出/覆盖失败停止。
按API/size/flags/归一化stream对齐，记录相对地址、对齐和free复用代次；
启动/完整请求/退出分段。异步free不等于GPU完成，跨线程进入顺序不等于
因果执行顺序。不同地址只是候选变量，相同地址也不证明物理布局相同；
不使用本轮TTFT更新性能门禁，不追加此家族样本。

执行前源码核对：cudaMallocHost仅router tracing路径使用，本请求关闭；
故要求其余六个普通路径API出现，不错误要求全部导入函数必然调用。
解析器接受560条CPU夹具，拒绝重复/缺失ID及畸形字段。既有CPU工具合同
身份未变直接复用。协议/驱动/解析器保存在evidence/hc-up-address-observer-20261017。
下一步执行固定两条并分析；生产main、原NO_GO和活动目标保持。

## 地址调查结果：调用关系相同但布局/复用不同（2026-10-17）

固定A/B各一次完成，输出与基线相同、覆盖通过、服务退出0，guard COMPLETED。
两侧各151958条API记录，75983次分配；startup/request/shutdown条数分别
1543/148872/1543。API/size/flags/归一化stream与free匹配代次的序列哈希
相同；无未匹配free、无逻辑live地址重叠，退出时均余8项（不据此判泄漏）。

119次启动cudaMalloc地址不同，858次请求cudaMallocAsync地址不同；后者
对齐余数也不同，调用ID范围1605–13163。不同异步地址按size分组：
17109248字节162次；167772160、100663296、786432字节各232次。
两侧复用次数总数均74394，但同一地址的累计复用代次数值27012项不同，
说明总次数相同不等于复用轨迹相同。相对首个device地址的75862项差异
受首地址基准移动影响，不能解读为75862处独立变化。

结论仅为布局证据：不能把它当成HC导致的差异，或TTFT因果证明。日志
扰动很大，插桩TTFT不参与性能门禁；此家族停止、不追加样本。
下一步源码映射这些buffer与生命周期，核对异步池复用/依赖策略是否提供
可单独操纵的变量，再冻结因果干预；不直接改分配策略或优化prefill。
CUDA本机头文件确实提供opportunistic/internal-dependency复用属性，但
尚未证明当前差异由其中任一策略导致，不预设属性修改为修复。
生产身份不变，原NO_GO与活动目标保持。

证据：evidence/hc-up-address-observer-20261017/address-review.json、
address-difference-breakdown.json及analyze.py；原始日志与完整归一化序列
在.q4t-work/hc-up-address-observer-20261017，已保存来源SHA。

## 地址差异的源码映射与干预选择（2026-10-17）

完整6600条prefill异步分配序列按24×8192+8181、48层（每4层1个full）、
每linear层6个scratch再每层1个routing metadata对齐，尺寸与顺序全部匹配。
这是完整序列源码推导，未采运行栈。858处差异：routing162，qkv_raw/qkv/
z/a/beta/y_ssm各116；chunk1/3/4/7/18/22分别58/163/207/178/236/16处。
其余chunk没有这些地址差异。首次chunk1层4 routing，最后chunk22层47。

- routing：quant/moe_gemm.cu:365–374，512专家、top-k10；T8192合计
  17109248字节。成功路径先join专家streams再combine/free；失败清理也
  要建立等待边。无需推断这些指针会被无序跨stream释放。
- linear：model/linear_attention.cu:1399–1434，宽度10240/10240/6144/
  48/48/6144，固定六项分配与析构释放顺序。对应167772160/100663296/
  786432字节。不是新增HC up的临时分配。
- 全部74432次异步分配和74432次异步释放在A/B均为stream0；额外MoE
  streams执行计算，但本次记录的池分配/free都在caller stream。

本机CUDA头文件将opportunistic定义为无依赖的已完成free复用，internal
依赖属性用于插入跨stream复用边。仅据本次全stream0记录，不能声称
关闭其中一个开关就能隔离所见地址差异；内部库不可见行为仍未知。
因此暂不运行盲目的pool属性A/B。下一候选干预应直接控制已定位七类
buffer的使用地址，同时尽量保留原分配/free调用与同步关系，以区分
使用布局与allocator/提交成本；必须先证明生命周期、别名与失败清理
安全，设置原地址直通负控，并冻结固定次数/判据，才运行真实HTTP。
这是诊断可行性方向，尚未实现/启动，不预设它会消除TTFT差异。

本轮仅离线映射，无GPU请求、无生产变更。原NO_GO与活动目标保持。
证据：evidence/hc-up-address-map-20261017/{map.py,result.json}。

## 固定scratch布局诊断工具完成CPU验证（2026-10-17）

新增scratch_layout_probe.cpp，仅接受显式control/fixed。两模式在首次成功
cudaMalloc后都保留562036736字节arena，直到进程退出；原cudaMallocAsync
与cudaFreeAsync均调用，参数/返回/errno转发。fixed将前6600次已验证
prefill分配的返回值改为7个独立2MiB对齐slot，free翻译回原allocation；
control返回原地址但执行相同跟踪与arena预留。decode前缀之后直通。
不改kernel或数学，不插入额外同步。它不是生产分配器，不可用于任意请求。

生命周期依据：linear六项在caller stream析构；routing在额外MoE streams
join后再free；观测所有async alloc/free均stream0。因此同slot下一次使用
仍在前次使用之后的stream顺序内。未知尺寸/顺序、slot仍live、非零stream、
外来线程直接INVALID中止；失败分配不读输出，失败free保留对应关系。
固定使用地址也改变承载数据的物理页与缓存局部性，不能称纯VA位实验；
两模式额外预留也可能改变驱动状态，必须使用直通control作负控。

CPU假CUDA：两模式完整6600分配/free、末chunk8181、分配失败无效输出、
free失败后重试、errno保持、固定slot稳定/独立、decode直通通过；每模式
shape/stream/foreign三项负控正确abort，共8个进程结果。零警告构建，
未加载CUDA驱动。夹具路径归档后未为路径移动重复运行同身份逻辑。
CPU通过不证明GPU生命周期/数值合同或性能；尚未冻结/运行真实实验。

下一步冻结A/B×control/fixed的交互判据与有限顺序，先满足整阶段guard、
输出合同和完整6600映射，再判断布局干预是否改变候选差异。不得只凭
fixed更快宣布原因，也不能将两模式一起变化的allocator成本归于使用地址。
生产main及候选原件未改，原NO_GO和活动目标保持。
证据：evidence/hc-up-layout-intervention-20261017/；工具及CPU夹具在tools/evalscope。

## 固定布局交互实验冻结（2026-10-17）

固定8条顺序：Acontrol/Bfixed/Bcontrol/Afixed / Afixed/Bcontrol/Bfixed/Acontrol。
每条件两次用于同身份负控R，不补样；原始200K off完整HTTP，各输出256。
整个驱动同一guard，只有scratch_layout_probe，无逐分配日志或事件插桩。
每条必须输出合同、容量、server退出和6600映射完成；首失败停止。

每四条一组计算control差D0=Bcontrol−Acontrol、fixed差D1=Bfixed−Afixed、
交互I=D0−D1。R为各同binary+mode两次差的最大绝对值。仅全部有效且
两组D0>max(R,1ms)、I>max(2R,2ms)才支持本干预下布局敏感交互；不将
D1小于R称等价、不以fixed绝对更快判因果。未达判据则停止此家族，
不扩样、不改门槛。即便达标，也不能直接证明历史因果/纯VA因素或接受HC。

协议、驱动、报告校验和分析已冻结到evidence/hc-up-layout-intervention-20261017。
CPU工具身份核对通过，旧CPU合同复用；新报告解析器接受原CPU日志并拒绝
计数不足、重复、arena尺寸错误和INVALID。生产main未改。下一步启动
固定序列；期间监控与结果持续保留，活动目标/原NO_GO保持。

## 固定scratch布局交互结果：判据未满足（2026-10-17）

固定8条全部完成，HTTP输出与基线一致、容量/服务退出检查通过、每条
6600分配/free映射完整；guard COMPLETED，无已检测竞争。原8条顺序与
脚本不变，无额外请求；生产main与原始A/B文件保持冻结身份。

| 指标（ms） | 第一组 | 第二组 |
|---|---:|---:|
| control B−A | −4.811 | −82.481 |
| fixed B−A | +300.192 | −192.506 |
| 交互 control差−fixed差 | −305.003 | +110.024 |

同binary+mode最大差R=371.694ms（来自两条Bfixed），冻结交互阈值
2R=743.387ms。control两组未复现稳定候选变慢，交互方向不一致且未
达门槛，故判据失败；不能指认scratch布局根因，也不能证明它无影响。
尤其不能把该干预下control差为负追溯成原历史NO_GO无效或只是噪声。

六条arena对齐基址0xfffca0000000，第5条Afixed、第6条Bcontrol为
0xfffcc0000000；
容量均562036736、slot偏移/2MiB对齐固定。此前两条地址观测只证明
布局不同，本干预未建立历史因果。固定布局改变实际使用的物理页及
局部性，但未锁定所有物理位置；两模式额外reservation和preload也可能
改变主模型加载布局。control不能等同未插桩原始main，禁止跨实验直接
把差值相减推断arena因果。

决定：停止该实验家族、不扩样、不交付诊断分配器、不修改生产prefill。
原NO_GO保持，候选完整数值/质量验收未补跑，不能接受候选。下一步先
对所有已尝试因果干预建立证据边界表，核对尚未控制的host加载布局与
主模型分配布局，明确能区分二者的新变量后才允许新GPU实验；不继续
堆叠已失败的观察家族。根因和最终解决仍未完成，目标保持active。

证据：evidence/hc-up-layout-intervention-20261017/interaction-review.json、
stage-audit.json；原始记录在.q4t-work/hc-up-layout-intervention-20261017。

## 因果证据边界与下一控制变量（2026-10-17）

| 已有证据/干预 | 实际约束 | 未证明或未控制 | 决定 |
|---|---|---|---|
| CUDA ELF与launch审计 | 原22 ELF、23资源字段及stream内顺序相同 | 参数地址、跨stream时序、物理状态 | 不再重复这些静态/trace检查 |
| 跳板消除 | 改已识别A53跳板，不改相对布局 | host绝对/相对布局、库加载、数据位置 | 因果门槛未过，停止该家族 |
| 取消HC注册 | 保持候选布局、仅跳过额外注册 | host布局及完整阶段独占（末条原ENV_ABORT） | 六条完整请求保留，判据未过，不补采 |
| Host/Gather/subtiming | 区间归属与有限观察 | 计时扰动；subtiming中断；非单一算子 | 不能把区间当原因，停止各家族 |
| 地址观测 | 分配/free关系相同，定位858处地址差异 | 历史run的地址、物理位置、身份到布局的因果 | 不重复地址观测 |
| 固定scratch交互 | 七类buffer固定偏移/对齐、原调用仍执行 | 额外arena/preload、加载布局、物理页；交互未过 | 停止、不交付分配器 |
| 遥测与计时源码 | 采样频率/系统计数、TTFT边界定义 | 瞬态/进程归属、历史映射 | 没有证据支持修改计时公式 |

上述阴性/未决结果均不等于相应因素无影响；也不能组合成“根因是噪声”。
已有可靠证据足以排除新增HC内核在off请求直接被调用的说法，不能排除
二进制加载与系统状态间接影响。原NO_GO保留，尚无可交付的因果修复。

新CPU可行性证据：本机`setarch aarch64 -R /bin/cat /proc/self/maps`连续
两次成功，完整maps文本相同。内核头文件ADDR_NO_RANDOMIZE标志用于
关闭进程地址随机化；未修改/proc/sys/kernel/randomize_va_space全局设置。
这提供了不重链接、不preload、不额外预留GPU内存的进程级控制变量。
但只证明CPU cat子进程可控制，不能宣称模型全部库/GPU映射会固定，
也不能宣称ASLR已解释差异。CPU验证记录见本阶段evidence。

下一步实现仅服务进程的no-ASLR启动控制及就绪时映射快照，CPU测试
确认客户端/父进程未继承修改，记录实际personality/maps而非只相信命令。
若服务映射仍不能核验稳定，则不进入耗时因果对照；即使稳定，CUDA数据
地址/物理位置仍须明确未控制边界。不得将setarch包住整个HTTP runner，
否则会同时改变客户端和工具布局。随后再冻结次数/判据，不预先启动GPU。

本轮无GPU请求、生产与候选身份不变；活动目标保持active。
证据：evidence/hc-up-cause-boundary-20261017/personality-cpu-check.json。

## 服务端地址空间控制工具完成（2026-10-17）

run_acceptance新增可选--server-address-space inherit|no-aslr，默认不启用。
no-aslr仅为服务命令添加setarch架构-R，保留真实binary身份；客户端命令
与父进程personality不变。inherit不加启动前缀，提供同样的就绪快照。
不支持与systemd隔离组合，明确拒绝而不是快照错误PID。

就绪时核对/proc/PID/exe、personality、前后start_ticks与父进程状态，
保存server-address-space.json及完整server-maps.txt；记录文件支持的可执行
映射范围/权限/偏移/device/inode/path。报告仅是就绪时虚拟映射，不能表示
物理页或全部CUDA分配。核验失败在HTTP前退出并清理server；不静默回退。
server-command/exit记录诊断模式，helper源码与请求证据一起存档；
performance_acceptance仍false，不因完整矩阵完成便接受诊断布局性能。

验证：4项CPU直接检查通过，包含两次真实no-aslr sleep子进程代码映射
相同、父进程及另一个inherit子进程未改变、缺少setarch/错误personality
拒绝；21项runner合同测试通过，包含仅server前缀、客户端不变、映射
失败先于HTTP且清理、systemd组合拒绝。未运行模型/GPU，无新性能结论。
下一步先冻结模型映射有效性检查与有限交互设计；不得假设CPU子进程的
固定映射自动外推到模型共享库，更不能据此将ASLR当根因。
生产main及原NO_GO保持，活动目标仍未完成。
证据：evidence/hc-up-aslr-20261017/。

## ASLR交互实验与模型映射门禁冻结（2026-10-17）

固定顺序Ainherit/Bno-aslr/Binherit/Ano-aslr / Ano-aslr/Binherit/Bno-aslr/Ainherit，
共8条原始200K off HTTP，各输出256。只有服务启动personality改变；
无preload、额外arena、事件计时、PMU/Nsight。runner/client保持原状态。
同binary+mode两次形成R；每组D0=Binherit−Ainherit、D1=Bno-aslr−Ano-aslr，
I=D0−D1。全有效且两组D0>max(R,1ms)、I>max(2R,2ms)才支持此控制下
personality相关交互，不自动证明历史根因，也不构成部署性能接受。

有效性除完整guard/HTTP合同，还要求实际bit状态匹配。两次同variant
no-aslr的文件支持可执行映射必须完全一致；第5/7条在HTTP前与先前
快照比较，不同立即停止并保留现场，不重跑到匹配为止。新增
--server-map-reference实现此门禁。inherit每variant两条应有不同代码映射，
否则缺少实际随机化对照，最终因果解释无效。映射只在就绪时获取，不能
证明请求期间所有延迟加载模块或GPU/物理页固定，结论边界明确保留。

工具修订验证：5项地址空间、22项runner CPU合同通过；新负控覆盖代码
映射偏移、身份或personality不符，映射错误在HTTP前清理并保留快照。
协议/驱动/分析与身份已冻结。GPU启动前核对空闲，完整驱动持同一guard。
首失败停止；固定8条后无论结果停止此家族，不扩样、不改阈值。
生产与原A/B身份不变，原NO_GO与活动目标保持。下一步启动执行。
证据：evidence/hc-up-aslr-20261017/。

## ASLR交互结果：控制有效，因果门槛未满足（2026-10-17）

固定8条均完成，HTTP输出/容量/退出通过，guard COMPLETED，无已检测竞争。
每条31个文件支持可执行映射；同variant no-ASLR重复完全相同，inherit
重复不同，personality与父进程状态符合协议。四条no-ASLR的30条共享库
映射记录跨A/B亦完全相同（29条唯一路径，不折叠同路径多个映射段）。
因此本轮启动时代码映射控制有效，不能把未达因果判据归为控制未生效。

| 指标（ms） | 第一组 | 第二组 |
|---|---:|---:|
| inherit B−A | +326.637 | +396.518 |
| no-ASLR B−A | −33.800 | +125.552 |
| 交互（前者减后者） | +360.437 | +270.966 |

同binary+mode最大差R=226.145ms（Binherit），冻结阈值2R=452.289ms。
两组inherit差均大于R，但交互均未超过2R，故frozen_interaction_gate_passed
为false。方向一致的衰减是线索，不是已确认的ASLR根因；不事后降低
阈值、不扩样、不把no-ASLR当成部署修复。no-ASLR的B自身仍差173.417ms，
说明就绪代码映射稳定不等于耗时稳定。GPU/物理位置、数据地址、请求中
延迟加载与硬件动态状态仍未控制。

决定：本实验家族停止，原NO_GO保持；无生产源码变更，不新增五档或
候选数值验收。下一步转向该线索的机制证据：先审计已有host计数器与
GPU busy/等待记录能否支持CPU提交或内存系统解释，以及可用计数器的
覆盖范围。只有能区分机制的新观测/干预才另冻结，不再同类ASLR计时。
当前仍缺历史因果闭合、对应修复与受影响验收，目标保持active。
证据：evidence/hc-up-aslr-20261017/interaction-review.json、stage-audit.json。

## 既有计数器的机制覆盖审计（2026-10-17）

离线重用host四条数据：两对user cycles B−A为−773860296/−288427150；
instructions +18190887/+5788343（同身份R16215008），branch misses
+397587/−780599。候选IPC约1.967/1.974，A约1.913/1.954；不能据此支持
稳定额外CPU工作量，但也不能证明所有提交/等待成本都不受影响。
这些计数仅当前线程user mode，CPU时间含kernel；cycles/CPU时间的约
1.62–1.63不能当CPU实际GHz。旧数据未采cache/TLB/stall，也不与ASLR
实验同轮配对，禁止将旧PMU与新TTFT拼接成ASLR的机理证据。

旧Nsight活动并集同样不提供SM-active周期、DRAM/L2等待；A存在竞争污染。
因此不能用既有GPU busy总时长反推出内存瓶颈或CPU提交根因。完成边界
审计后，不重新运行这两个已停止的观察家族。

本机sysfs提供nvidia_ucf_gpu_pmu_0与nvidia_ucf_pmu_0的cycle/read/request/
outstanding等事件。只对前者以disabled=1、system-wide CPU0打开cycle事件
（未启用采集、未提交GPU工作），perf_event_open返回EACCES=13；当前
perf_event_paranoid=2，未修改系统权限或sysctl。事件名存在不代表当前
权限可采集，也不代表event语义已完成验证，不能将缺数据记为零。

本机Nsight Compute 2026.2.0.0存在，已读取本地help/ProfilingGuide；默认
kernel replay、cache-control=all、clock-control=boost。该工具可能重放、
保存恢复GPU数据和改变缓存/时钟，不能直接启动完整profile后把结果当
原始调度证据。下一步限定为计数器采集方案可行性核对：明确权限、目标
kernel/区间、single-pass与cache/clock控制，以及它能区分的假设；不先
做新HTTP再寻找解释，也不将更多普通A/B计时当机制证明。

本轮无模型/GPU请求、无生产代码变更；stage-audit仅能力与证据边界，
根因仍未闭合，原NO_GO与目标active保持。
证据：evidence/hc-up-mechanism-audit-20261017/。

## 计数器权限与采集扰动边界核实（2026-10-17）

在已有NOPASSWD sudo授权下，复用已审阅的check_uncore，仅以disabled=1
打开type12/config0x100000000事件后立即关闭：成功，errno=0。普通用户
EACCES历史证据保留；不能再把权限描述为整个诊断的阻塞。未启用计数、
未启动模型或GPU工作、未改perf_event_paranoid=2或RmProfilingAdminOnly=1。

本机Nsight Compute文档确认普通kernel profiling会串行化launch；range
模式需另行验证。CLI默认cache-control=all、clock-control=boost，另有
pipeline-boost-state=stable。故直接全量profile会改变待解释的执行条件；
该选项存在不等于基线Tensor Core状态已知，也不是TTFT根因证据。

下一步优先核实外部UCF采集的事件语义、分组计数覆盖与读取边界，避免
先启动模型再找解释。UCF为system-wide，不能直接当每kernel的stall或
进程专属流量。具备可区分假设的方案后再冻结诊断；停止的ASLR等计时
家族不重启。当前没有生产修改或性能接受结论，原NO_GO与active保持。
证据：evidence/hc-up-mechanism-audit-20261017/uncore-admin-access.json、
capability-review.json；本轮仅能力检查，不重复已有模型验收。

## UCF四事件分组能力验证（2026-10-17）

冻结一次200ms、无CUDA/模型负载的检查，在GPU共享锁内运行。cycles、
rd_bytes_loc_rem、rd_req_loc_rem、rd_cum_outs_loc_rem四事件组成pinned
组；read长度和4个ID全部匹配，enabled=running=200111402ns，无复用。
原始值依次300717420、5929472、93328、8884745200，guard COMPLETED。
代码以-Wall -Wextra -Werror构建通过；没有改变生产二进制或重跑HTTP。

[Thor官方文档](https://docs.nvidia.com/jetson/archives/r38.2/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/PerformanceMonitoring.html)
限定UCF-GPU统计流经UCF的集成GPU物理地址流量；不是SM stall或进程
专属计数。未提交负载时计数仍非零，不把背景当零，也不将outstanding
原始累计量直接换算成TTFT。Grace的同名事件和上游master驱动不能替代
本机Thor事件单位与覆盖验证；当前仅确认分组可用，没有根因结论。

下一步实现外部请求区间快照、保留ID与enabled/running检查，再校准事件
响应后决定机制对照；不能以采集器可用直接开启更多A/B计时。已有数值/
性能证据复用，原HC NO_GO与目标active保持。
证据：evidence/hc-up-ucf-group-20261017/。

## 外部UCF请求窗口采集器落地（2026-10-17）

新增tools/evalscope/ucf_window_probe.cpp，独立构建，不链接或注入服务器。
读取实际PMU type并验证CPU0 mask，以pinned四事件组提供单次start/read/
stop协议。记录CLOCK_MONOTONIC操作边界、原始值、事件ID与enabled/running；
校验完整read、ID和无复用覆盖。只允许单窗口，避免reset计数却未reset
累计时间造成错误覆盖率；EOF/错误命令退出失败并关闭所有事件fd。

在共享GPU锁下固定验证3种生命周期：正常窗口两次快照、重复start、
采集中EOF，均符合预期；guard COMPLETED。两快照enabled=running分别
20126341/40224443ns，read边界宽10537/1703ns；仅代表本次工具调用开销，
不证明HTTP集成后的整体扰动。初次-Werror发现time_t窄化，修为显式转换后
-Wall -Wextra -Werror零警告。没有CUDA/模型负载，没有重跑性能或数值验收。

该工具补齐外部区间观察能力，尚未接入HTTP；UCF事件负载响应与单位仍
须校准。下一步用有明确读写量的有限诊断验证计数覆盖，再决定是否值得
冻结原二进制的机制观察。不得把采集器通过当根因成立或HC可合入。
原NO_GO及活动目标保持；生产源码/二进制不变。
证据：evidence/hc-up-ucf-window-20261017/。

## UCF已知GPU读流量校准通过（2026-10-17）

固定同一CUDA进程、空闲20ms/256MiB拷贝一次/四次各一条，运行前冻结
判据：快照全有效；一次raw read-byte >128MiB且>10倍空闲；四次/一次
在[2,6]。初始化与kernel首次解析在计数前，输出全量0x5a校验在计数后。
没有测吞吐或用微测接受性能实现；该诊断仅验证观测工具的负载响应。

三窗rd_bytes原始值705024/268453376/1074083840，四次/一次4.001007；
冻结判据通过，所有快照无计数复用，输出全量校验通过，guard COMPLETED。
CUDA夹具以-Wall -Wextra -Werror构建通过。它支持本机该事件观察GPU经
UCF的读字节量；不证明所有kernel覆盖相同，不校准write/outstanding单位，
不能用累计outstanding直接换算TTFT，也不构成HC根因证据。

下一步接入明确的请求前/首token边界，外部采集保持服务器原二进制、
默认地址空间与调度；必须明确首token后边界误差，不把客户端进程启动/
模型加载/全部decode混进prefill计数。该机制观察需要另行冻结次数和判据，
不恢复停止的普通计时家族。本轮未重复HTTP、未改生产代码，原NO_GO、
数值合同和目标active保持。
证据：evidence/hc-up-ucf-calibration-20261017/。

## EvalScope首有效输出边界钩子（2026-10-17）

核对已安装DefaultApiPlugin：请求JSON序列化后才记录st，首有效输出
TTFT使用解析该SSE JSON前的timestamp；空role/心跳不触发TTFT。新增
ucf_client_boundary.py，只生成该函数的诊断副本，不修改venv或服务器。
严格校验模块SHA256和两处AST锚点，start在原st之前，first在原TTFT赋值
之后，保留原计算时间戳。源身份或锚点变化时拒绝插入，不静默适配。

四项CPU假HTTP验证通过：心跳/空role/分段SSE/后续token只触发一次；
无内容不触发；源码身份变化拒绝；回调异常在EvalScope结果中明确失败。
发现原API会捕获回调异常，封装必须同时检查结果与观测完整性，不能只看
CLI退出码。没有启动GPU/计数器/模型，没有重跑已通过验收。

尚未接入CLI或runner。下一步封装单请求collector生命周期与原始证据保存，
记录首片段时间戳到停止ioctl的延迟，并验证失败路径。首token到达客户端
之前服务器可能已开始decode，故该观测只能称HTTP首输出窗口，不能冒称
严格纯prefill；边界误差须纳入机制解释。原NO_GO和active目标保持。
证据：evidence/hc-up-ucf-boundary-20261017/validation.json。

## UCF客户端封装与runner诊断入口（2026-10-17）

新增ucf_evalscope.py：原客户端身份校验后安装首输出钩子，sudo只用于
外部counter。封装单请求生命周期，超时/EOF/重复请求/无首输出均失败；
检查原始快照、四事件config/ID、无复用覆盖及子进程退出，保存原始响应、
probe SHA、请求前计数时长和首输出至stop的延迟。与perf_counter使用
相同CLOCK_MONOTONIC实现才允许跨进程时间比较。未修改venv或服务器。

run_acceptance新增显式--ucf-probe，只允许selected-tier单请求客户端，
归档工具源码、标记diagnostic_ucf和performance_acceptance=false。即使
客户端成功退出，缺少complete计数记录也使runner失败，避免静默漏采。

CPU验证：6项collector协议、2项真实EvalScope+假counter集成、2项runner
合同通过。集成测试首次发现继承导致6项重复执行，已移除重复发现结构；
runner负控首次误断言不存在的passed字段，改为http_output_checks_passed后
仅重验该失败项通过。所有测试均无GPU/HTTP模型工作，旧性能合同未重跑。

下一步冻结有限主线/HC机制观察：保持原服务器binary和默认布局，不启用
profiling/层计时；以读字节量及UCF原始计数区分流量是否增加，明确HTTP
首输出边界误差门槛和停止条件。尚无真实HTTP硬件观测，也没有根因或
修复结论；数值合同、普通decode要求、原NO_GO与active保持。
证据：evidence/hc-up-ucf-client-20261017/validation.json。

## UCF机制观察协议冻结（2026-10-17）

冻结4条ABBA，原main/HC二进制SHA身份不变，200K off固定fixture/output；
服务器不加preload、层计时、profile或地址空间覆盖。外部四事件计数覆盖
HTTP请求前至首有效输出，整阶段共享GPU锁及竞争/环境遥测。

有效性要求：HTTP输入输出/容量/退出合同、计数组完整且无复用；计数开始
到请求st、首输出timestamp到stop_end均在0–1ms。失败立即停止并保留，
不重复已成功请求。四条结束不扩样。主要判据：两对B−A读字节差均大于
max(R_bytes,1GiB)，R为同variant重复绝对差最大值；通过也只允许针对性
因果干预，不能直接接受HC。cycles/requests/outstanding只报告原始信息，
不事后挑门槛，不把outstanding换算TTFT，不以新TTFT更有利翻转NO_GO。
HTTP首输出窗口含潜在早期decode与系统背景，明确不是纯prefill。

该观察回答尚缺的“相同kernel调用下GPU读流量是否增加”，不是重复普通
性能采样。工具校准和CPU失败测试复用；本提交为执行前冻结边界，尚未
开始这4条。原NO_GO、数值合同及active目标保持，结果随后追加记录。
证据：evidence/hc-up-ucf-observer-20261017/protocol.json、run.py。

## UCF机制观察完成：额外读流量判据未过（2026-10-17）

固定ABBA四条正常结束，guard COMPLETED，802次monitor无已检测竞争。
原binary身份/输出/容量/退出全过；外部计数完整且enabled=running。请求前
边界0.174–0.186ms，首输出至停表0.081–0.108ms，均满足冻结1ms限制。
四条输出与既有fixture参考逐字节一致。离线analyzer初次写错硬编码输出
hash，改为从身份不变的参考results派生并逐条比较后通过；仅修复分析，
没有重跑模型。错误与更正另存analysis-correction.json。

| 请求 | TTFT(s) | UCF读字节 |
|---|---:|---:|
| 01 A | 158.859958117 | 11038569989120 |
| 02 B | 158.928028092 | 10839922244608 |
| 03 B | 158.972249389 | 10852745123328 |
| 04 A | 158.895210614 | 10840801239040 |

两对B−A读流量−198647744512/+11943884288 bytes，方向不一致；同身份
R=197768750080 bytes，冻结门槛max(R,1GiB)=R，判据false。TTFT两对
+68.070/+77.039ms，但不能据此把差异归于额外读流量。该结果仅不支持
本次冻结的流量机制，不能排除访问时序、延迟或调度影响，更不能把
outstanding累计值直接当stall时间。所有原始counter保留，不挑选子集。

决定：停止UCF观察家族，不扩样、不事后降低门槛。原HC NO_GO保持，
生产main未改，仍缺因果闭合、对应解决与受影响验收。下一步聚焦尚缺的
干净设备活动/提交间隙区分证据：先核对旧trace污染边界和最小采集方案，
不把已有污染trace的耗时当对照，也不继续普通TTFT或UCF重复采样。
证据：evidence/hc-up-ucf-observer-20261017/review.json、各请求counter及lease。

## 输出hash记录纠正与干净trace协议冻结（2026-10-17）

源码复核纠正上一条analyzer说明：不是写错参考hash，而是把results.outputs
已经存储的SHA256再次哈希，错误标成output_sha256。现直接比较参考hash，
并独立读取四条原始output-0.txt计算验证，实际均为57f3581d…8a4c8ca。
runner本身一直正确验证输出；UCF门槛和结论不变。原错误说明保留历史，
修正review和analyze.py，并新增output-hash-correction.json；没有GPU重跑。

旧Nsight A为254.629s、B160.412s，A首chunk约93.662s空隙与其他模型
加载重叠，后续残留未量化；旧耗时比较从未通过干净环境验收。静态
kernel序列/资源属性证据继续复用。冻结新的最小A/B各一次CUDA trace，
两条同一GPU独占阶段，模型就绪后采集，sample/CPU context-switch关闭，
不启用UCF、层计时或NCU重放/缓存/时钟控制，原服务器二进制不变。

分析从首EmbedLookup到首496640B D2H结束，按25chunk划分进程活动并集
及补集，校验2264679prefill kernels和输出/退出/导出。只用于区分设备
活动与提交间隙线索；若profile差异方向与原回归相反，不用于原回归归因。
这是补齐旧污染导致缺失的同阶段时序证据，不是重做已有静态合同。两条
结束或首个有效性失败即停，不扩样，不用profile吞吐接受HC。当前只冻结
协议尚未运行；原NO_GO、生产main与active目标保持。
证据：evidence/hc-up-clean-trace-20261017/；hash修正见UCF证据目录。

## 干净trace启动被guard误报中止，CPU复现定位归属缺陷（2026-10-17）

执行冻结协议时，A在模型启动阶段被guard ENV_ABORT，未发HTTP、B未启动，
没有完成新的trace或性能请求。Nsight目标q4t PID1223417、PGID1223417，
父PID1223416；guard仅按自身PGID归属，将其判作foreign。原ENV_ABORT
保留，不能算环境/模型验收通过。核实nsys session列表为空、目标/父PID
均已不存在，本阶段已终止且释放锁，不以观测超时推断进程停止。

在共享锁内做一次无GPU/CUDA的nsys launch --trace=none Python子进程
复现：目标PID1223574自行setsid，PGID/SID均1223574；父nsys-launcher
PID1223573仍在driver PGID1223542，祖先链完整。目标继承诊断环境标记，
但无需据此放行；可用实际父子关系证明所有权。CPU复现guard COMPLETED，
会话按唯一session关闭。该证据定位了监控兼容缺陷，不是TTFT原因。

下一步修复guard仅认同PGID的问题：使用当前进程树和PID start_ticks
识别同阶段后代，维护受控退出清理；PID复用、不可读/未知GPU进程仍失败。
先做该缺陷的CPU/进程生命周期验证，再恢复尚未完成的trace；不放宽按名
过滤、不复跑已成功HTTP项。本轮生产代码和数值合同不变，原HC NO_GO、
根因未闭合及目标active保持。
证据：evidence/hc-up-clean-trace-20261017/lease-abort、
evidence/hc-up-nsys-ownership-20261017/。

## Guard独立session后代归属/清理修复及trace恢复协议（2026-10-17）

gpu_exclusive用当前进程树证明自有后代归属，记录PID+start_ticks；已证明
身份reparent后仍可跟踪，复用PID或缺少启动时间不继承所有权。新子节点
必须不早于当前父节点启动，不能因复用父PID而误放行。没有进程名白名单。
退出前再次收集后代，独立session使用pidfd并复核start_ticks后发信号，
保留锁等待退出；无法验证清理身份仍失败。另修复清理异常仅改状态却
可能返回0的缺陷，现在明确返回75 ENV_ABORT。

18项CPU guard合同通过，包含真实setsid/拒绝SIGTERM子进程清理、PID
复用/父时序负控和无关进程保留；清理退出码修复后针对3项重验通过，
共19个独立用例。真实nsys --trace=none CPU子进程复现归属通过，模拟
外部GPU PID仍产生conflict；全程无CUDA/模型请求。guard COMPLETED。

恢复干净trace使用新v2目录，保留旧ENV_ABORT。只更新已修复guard SHA，
仍原A/B各一次、相同binary/fixture/profile工具/判据；原阶段没有成功
HTTP，不构成重复已通过请求。先提交冻结恢复协议，再在共享锁内执行。
监控修复不是TTFT根因修复；原NO_GO和active目标保持。
证据：evidence/hc-up-nsys-ownership-fixed-20261017/、
evidence/hc-up-clean-trace-v2-20261017/。

## 干净trace完成：差异落在设备活动并集（2026-10-17）

冻结A/B两条均完成，输出一致且匹配既有参考，退出/导出通过；guard
COMPLETED，532次monitor无已检测竞争，自有Nsight后代归属修复有效。
两个trace各2264679次prefill kernel，23项资源属性计数及四stream内序列
一致；25chunk覆盖通过。保存原始trace位置/SHA，不覆盖旧污染trace。

HTTP TTFT A160.793368123s、B160.848453478s，B−A+55.085ms。
首EmbedLookup至首496640B D2H设备区间B−A+52.864ms，其中活动并集
+90.104ms，间隙−37.240ms；下一API进入前间隙−38.286ms。此干净profile
方向与普通运行回归相同，额外时间位于进程GPU活动并集，而非更长空隙。
仍不能排除host提交节奏影响并发中的设备执行，也不是整卡idle测量。

kernel耗时总和B−A+227.373ms，因并发重叠不能当wall。主要正差分散于
两类nvjet OOTST(+56.776/+53.151ms)、GDN(+41.691ms)、SwiGLU量化
(+41.621ms)、CombineWithGate(+26.499ms)、CausalConv(+23.961ms)；
另一些GEMM/attention反向。不能只挑正差kernel或把总和当根因。

两条采集家族按协议结束，不追加。下一步仅离线复用这两个trace拆分
并发与独占活动，区分重叠变化及独占执行变长，再设计有针对性的因果
干预。当前仅定位到设备活动时间，尚未解释为什么变慢；原NO_GO、
生产main身份与active目标保持，不宣布根因闭合或性能接受。
证据：evidence/hc-up-clean-trace-v2-20261017/review.json、device-audit.json、
launch-audit.json、kernel-sums.json及retained-traces.json。

## 干净trace并发/独占拆分与同位置配对（2026-10-17）

只读既有两份SQLite，按活动start/end扫线；手工可算的嵌套/接触边界/
同名重复区间校验通过，busy/gap分别与上一独立并集算法逐ns一致。将
+90.104ms活动并集拆为仅一个活动+41.562ms、多活动重叠+48.542ms。
不能把重叠次数变化或partial-exclusive时间当完整kernel因果贡献。

GDN900、CausalConv900、CombineWithGate1200次调用在A/B中均全部
独占被追踪活动；各自耗时仍增加41.691/23.961/26.499ms。因此“全部
只是并发kernel变慢”的解释不足；但历史缓存状态、实际时钟及隐藏活动
仍未控制，独占不是因果证明，更不等同排除host先前提交节奏的影响。

按25chunk及层内相同序号配对，GDN正/负543/357，Conv461/439；
Combine1004/196，48个层序号累计差均正，前24个8192-token chunk均正，
最后8181-token chunk差−0.0104ms。Combine模式最一致，可作为具体
排查对象，尚不能据shape差异就认定地址/缓存冲突或挑有利子集作接受。

下一步先从src/model/hyperconnection.cu的CombineWithGate读取参数沿
调用链映射缓冲区，复用既有分配记录确认哪些地址/布局已经覆盖、哪些
没有，再选择受控干预。当前没有新增GPU请求或生产改动；原NO_GO和
active目标保持，仍缺根因、对应解决及受影响验收。
证据：evidence/hc-up-concurrency-audit-20261017/。

## Combine参数与持久分配映射（2026-10-17）

沿Model::RunLayers→DecoderLayerForward→mlp_frame.Write追踪，Combine的
block_output/R/inject_gate分别是d_ws内layout.block/combined/gate，out为
零基偶数层d_trunk2、奇数层d_trunk。attn WriteAndRead已把MLP residual
写入combined，MoE输出写block；gate由持久gate_storage提供，不是临时
cudaMallocAsync。该来源与旧固定七scratch实验不重叠，不能宣称已排除。

复用旧address observer，匹配完整唯一12次persistent allocation尺寸/顺序
及紧随的workspace，共13个角色。d_ws为2348023808 bytes，trunk各
167772160 bytes。A/B三个输入共用相同虚拟workspace地址0xffe120000000；
d_trunk2同为0x32a800000，d_trunk为0xfffd55a00000/0xfffd35a00000，
B−A=−536870912（512MiB），三个大buffer都2MiB对齐。其他12个角色
虚拟地址相同。映射来自完整调用顺序，不是仅按尺寸猜测。

这组地址不是新clean trace同轮，不能拼成因果证据；相同VA或对齐也不
证明物理页/缓存状态相同。新trace中Combine偶数层累计+13.295ms、奇数
层+13.204ms，近似对称，不支持直接宣布“只因奇数层输出VA不同”。
因此不据此立即开展只移动d_trunk的试验。下一步核对持久buffer布局的
可控边界，明确如何区分VA、物理分配与缓存历史，再冻结针对性干预；
不把未覆盖区域当成已证明原因，不继续泛化计时。

本轮仅源码/旧记录分析，无GPU请求、无生产改动。原HC NO_GO、数值与
普通decode要求、active目标保持；仍缺因果闭合及解决。
证据：evidence/hc-up-combine-map-20261017/。

## VMM可控边界实测：新分配可别名，旧cudaMalloc不可直接保页重映射（2026-10-17）

固定一次小内存能力检查，shared GPU guard COMPLETED；-Wall -Wextra
-Werror构建通过，无模型/吞吐测量。设备支持VMM，最小粒度2MiB。
普通cudaMalloc 2MiB的cuMemRetainAllocationHandle返回1（INVALID_VALUE）；
新cuMemCreate的同一2MiB handle映射两个不同VA，retain值相同，两个
方向的全量写读校验通过，之后unmap/free VA/release handle/context成功。

本机CUDA13.3头文件说明retain地址需先由cuMemMap映射；实测不支持把
当前legacy buffer直接保留物理页后改VA。VMM新分配可以解耦backing和
VA，但从cudaMalloc改成VMM本身是另一变量，不能把重新分配后的收益
当成纯VA原因。固定VA也不等于跨进程固定物理backing或缓存历史。

[CUDA VMM官方说明](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/virtual-memory-management.html)
还要求alias写入完成后，按stream/event顺序启动的后续操作才可经另一
alias读取；同grid/并发grid不能无条件混用。当前检查在操作间同步，不能
推广为任何同时alias访问都正确，后续必须保持这些数值/顺序合同。

下一步核实新VMM backing跨顺序进程共享的能力，以便针对d_ws/trunk
设计物理backing与VA分开的对照；此方案仍须区分分配方式变化，不能
只重复随机layout。尚未冻结任何模型实验或修改生产allocator。原NO_GO、
active目标与数值要求保持，根因仍未闭合。
证据：evidence/hc-up-vmm-capability-20261017/。

## VMM跨进程共享backing能力验证（2026-10-17）

为区分持久buffer的VA与实际分配，固定一次2MiB POSIX FD共享检查。
owner保留同一cuMemCreate handle，两个fresh exec子进程顺序导入，分别
精确映射0x4000000000和0x4020000000（相差512MiB）。owner写0x11，
child1全量读后写0x22，owner全量确认，再由child2全量读后写0x33，owner
再次全量确认；跨进程交接前均同步。两个子进程、owner清理均成功，
shared guard COMPLETED，-Wall -Wextra -Werror构建通过。

这证明当前设备能跨顺序进程保持同一VMM backing并改变VA，不证明
legacy cudaMalloc布局导致TTFT差异，也不控制缓存历史或其他模型分配。
本轮没有模型请求、没有重跑已通过性能/数值项，没有生产代码改动。
下一步设计覆盖d_ws及双trunk的诊断对照，须同时控制分配方式、backing、
VA及执行顺序，先冻结可区分假设的判据，再决定是否开展模型实验。
当前仍无根因闭合或生产修复，原HC NO_GO和active目标保持。
证据：evidence/hc-up-vmm-ipc-20261017/。

## 持久buffer诊断接入与CPU合同（2026-10-17）

新增独立LD_PRELOAD工具persistent_layout_probe.cpp，不改生产源码或
原A/B可执行文件。复用旧allocation observer验证A/B startup的1541次
cudaMalloc尺寸序列完全一致，完整清单固定身份；目标零基序号1534、
1535、1537分别为d_trunk、d_trunk2、d_ws。工具逐项核对前置及后续
序列，不能仅凭167772160/2348023808大小匹配；任何不符终止诊断。

original/shared两种模式均转发、保留全部legacy分配，并导入FD73–75
和映射相同三个VMM区域；只有返回给模型的目标指针不同。基地址依次
0x4000000000、0x4100000000、0x4200000000，可整体加512MiB；精确地址
不可用则失败，不回退随机地址。释放时先同步、转译到原指针，再释放
该slot映射/handle；startup_complete和cleanup_complete必须由外层验收。
工具不生成、清零或持有跨进程backing，必须由后续owner驱动负责。

真实driver链接版本-Wall -Wextra -Werror构建通过。fake CUDA CPU验证
9项通过：original/shared×两组VA四项全路径，前置序列错误、目标之间
序列错误、清单截断、legacy分配失败、精确VA失败五项负控。正常路径
检查返回指针、free转译、errno和3次同步/unmap/release；负控不能发出
cleanup_complete。输入清单与旧记录SHA随证据保存，没有GPU/模型测试。

此工具尚不具备完整实验驱动，不可单独运行模型或作接受证据。仍需实现
backing owner的跨子进程生命周期、二进制/输入/工具身份及完整输出检查，
并冻结分配方式/backing/VA/执行顺序的可辨识对照；不能把shared变快
直接归因物理页或VA。未查清根因，原HC NO_GO和active目标保持。
证据：evidence/hc-up-persistent-control-20261017/。

## 持久backing owner及HTTP描述符传递（2026-10-17）

新增persistent_backing_owner.cpp，持有三份VMM allocation及POSIX FD，
通过posix_spawn启动完整阶段驱动并传入FD73–75；等驱动结束后逆序释放，
保留驱动退出码。使用CLOEXEC、检查目标FD未占用，避免覆盖已有描述符。
owner不映射、不清零、不提交kernel；由同一GPU guard覆盖整轮driver。
真实CUDA链接构建零警告通过，目前尚未运行真实owner GPU集成。

现有runner的Popen默认关闭继承FD，故新增显式--persistent-backing-fds：
启动前检查三个FD存在，只向服务器传递，记录诊断身份；默认路径不传。
该入口限单条200K off、max_len208896、无systemd，不能当完整性能验收。
中间阶段driver仍须以pass_fds保留描述符，不能只在owner端设置继承。

fake CUDA owner三项CPU检查通过：两次fresh子进程共享相同memfd且数据
连续可见，driver退出0/7均清理并原样返回；spawn失败拒绝且无成功标记。
runner四项检查覆盖转发、FD缺失、错误shape拒绝、默认不转发。首轮3过，
正向用例因mock整个os.fstat破坏shutil.copyfile而失败；修正为仅mock
73–75，其余委托真实函数，仅重验该失败项通过。未重跑未变化的3项。

下一步验证真实CUDA下owner+probe三份大buffer的接入、跨进程可见性和
释放，再冻结模型对照顺序与因果门槛；工具通过不代表TTFT归因或性能
接受。本轮无GPU模型请求、无生产计算路径改动，原NO_GO/active保持。
证据：evidence/hc-up-persistent-owner-20261017/。

## 真实VMM全尺寸接入通过，持久placement对照冻结（2026-10-17）

owner+probe在真实CUDA下固定一次、两个fresh子进程顺序接入通过。
三份有效buffer共2683568128 bytes，各进程全量写后读回；第二进程在
改写前还全量确认第一进程pattern，VA整体偏移512MiB。两个startup/
cleanup标记、owner释放、退出、guard COMPLETED均通过。legacy runtime
分配使用fake地址，故该检查只验证真实VMM数据/生命周期及probe转译，
不覆盖模型实际allocator压力，也不是性能测试。无模型/吞吐采样。

冻结下一阶段8条HTTP：A-original、B-shared、B-original、A-shared，
再反序四条。每条fresh服务器，同一owner保留三份backing及完整guard。
两臂均保留legacy分配、导入相同backing和固定VA；仅返回目标指针不同。
没有PMU/Nsight/事件计时。全部输出、输入、容量、退出、身份及工具标记
需通过。R取四个cell各两次TTFT差的最大绝对值；两半均要求原臂B−A>0、
交互(D_original−D_shared)>2R、abs(D_shared)<=R。失败即停止该家族，
不增样或降门槛；通过也仅支持此分配条件下的联合placement贡献，仍不能
把VA/物理页/缓存历史细分或宣称生产可接受。协议在运行前保存。

当前根因尚未查清、生产未改，原HC NO_GO/active保持。下一步执行这组
有界对照，按结果决定是否有依据进一步分解placement因素。
证据：evidence/hc-up-persistent-integration-20261017/及
 evidence/hc-up-persistent-interaction-20261017/。

## 持久placement八条完成：有效性通过，因果判据未过（2026-10-17）

冻结8条全部完成；同一owner贯穿，guard COMPLETED、1601次monitor未检出
竞争/读取失败，各请求输入/输出/容量/退出及三slot cleanup均过，输出
仍为57f3581d…a4c8ca。全程使用原A/B二进制，生产计算源码未改变。

同cell最大重复差R=238.727ms，冻结2R=477.454ms。前/后半原路径
B−A=+99.116/−173.023ms，共享路径=+7.405/−178.373ms，交互分别
+91.711/+5.350ms，均未超过2R；后半正控制也失败。不能挑前半“差异
缩小”归因，不能把共享残差在R内当成根因闭合。停止该placement家族，
不追加请求、不降低门槛，不将诊断分配器用于生产。

该干预覆盖此前七scratch未覆盖的d_ws和双trunk，但仅检验当前新增VMM
资源条件下的联合placement影响；失败不是证明所有地址/物理页/缓存因素
绝对无关，也不推翻原候选NO_GO。CPU分析脚本四项算术/部分行拒绝用例
通过，最终只在完整八条和终态guard齐备后运行。原始HTTP保留本地并记录
逐文件SHA，关键结果/服务器日志/监控/协议随版本保存。

下一步先离线复核旧registration干预实际覆盖的device模块装载与地址
边界，区分已经否定的注册成本解释和是否尚有指令侧变量未被控制；
没有具体可辨识机制前不启动新模型实验。根因仍未闭合，无生产解决或
性能接受，原HC NO_GO及active目标保持。
证据：evidence/hc-up-persistent-interaction-20261017/。

## 注册干预与device模块证据覆盖复核（2026-10-17）

只读核对原A/B/C完整SHA和实际文件差分：B→C仅文件offset36896–36899
四字节改变，即VA0x9020入口RET；所有非.text有文件内容的section一致，
包括嵌入fatbin。没有重链接，也没有移除磁盘里的HC设备代码。该干预改变
运行时注册状态，不能由host布局不变推导GPU装载地址不变。旧guard仍为
ENV_ABORT，数值门槛未过；此次审计不改写或重跑旧实验。

分别查询干净A/B trace的实际SQLite schema：只有kernel/runtime/copy/
memset/event/synchronization/overhead活动，没有module/function表，
kernel记录没有module或address列；trace从ready后才开始。旧注册实验
环境只记录CUDA_HOME与Q4T_LAYER_TIMING，没有显式CUDA_MODULE_LOADING，
不能根据变量缺失判断实际LAZY/EAGER行为。因此现有证据没有回答“未调用
的HC模块是否实际装载”，更不能排除所有指令侧状态。

本机CUDA13.3 CUPTI头文件提供MODULE_LOADED资源回调及moduleId、
cubinSize、pCubin；pCubin是回调期间有效的host镜像指针，不是GPU指令
地址。下一步可做有界启动期模块观察并对cubin哈希，先确认装载是否发生，
而非直接新增性能对照。该观察只能补装载事实，不能单独证明TTFT原因。
本轮无GPU请求、无生产改动；根因与解决仍未闭合，NO_GO/active保持。
证据：evidence/hc-up-module-coverage-20261017/。

## 启动模块观察器实现与真实CUPTI校准（2026-10-17）

新增module_load_probe.cpp，仅订阅MODULE_LOADED资源回调，回调内同步
计算host cubin的SHA256并记录moduleId/context/size；不调用CUDA、不采
性能计数、不做replay或时钟控制。显式启用、空descriptor、非法cubin、
CUPTI错误均fail closed；退出unsubscribe并输出完整记录数。链接本机
libcupti与libcrypto.so.3，不要求额外安装开发包。

-Wall -Wextra -Werror构建通过；fake CUPTI+真实SHA四项CPU检查通过，
覆盖两线程16条唯一序号/正确哈希、缺descriptor、订阅失败、未显式启用。
固定一次真实GPU校准：已知kernel写42并读回，MODULE_LOADED记录1条，
5680-byte镜像SHA与cuobjdump提取的目标cubin完全一致，complete=1、
退出0、guard COMPLETED。仅工具校准，不是模型/吞吐验收。

下一步冻结原A/B各启动一次到ready后退出的观察，记录启动模块哈希及
是否出现HC独有镜像；不发HTTP或计启动性能。它仅补启动期装载证据，
不能代表请求期间从不装载，也不暴露GPU指令地址或证明TTFT因果。
原NO_GO、数值要求与active目标保持，无生产修改或已通过测试重跑。
证据：evidence/hc-up-module-observer-20261017/。
