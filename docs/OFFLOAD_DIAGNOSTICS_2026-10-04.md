# Offload短档解码与整体RAM诊断（2026-10-04）

## Goal与冻结边界

用户已授权下一阶段Goal。入口f15190f，隔离工作分支
codex/offload-diagnostics-20261004；原工作区七处修改逐项核对后
独立保留。上一轮完整性能NO_GO继续成立，本轮交付诊断证据与
下一项最小修复建议，候选保持默认关闭，不做新策略性能接受。

证据根为主工作区`.q4t-work/offload-diagnostics-20261004/`；入口见
entry.json，预先冻结的样本/假设/出口见plan.json。隔离源码位于
该目录source，HTTP产物在source/.q4t-work/evidence/。

## 有界任务队列

1. 成组补默认关闭、严格开关的真实请求阶段诊断。保存request ID、
   prefill开始、prefill结束/真正decode开始、计算结束的累计统计，
   首1/8/32个decode forward的轻量快照，以及外层边界GPU/L2/mirror
   resident ID和recency。子块T_sub形状和真实请求阶段分别记录；
   不读权重payload、不新增GPU同步、不改变调度/分区/GEMM。
2. 干净源码本机Thor零警告构建。第一项测试为tools/evalscope固定
   11题HTTP，partition1、诊断1、已有residency timing1；之后统一
   新增/受影响工具合同。代码身份变化的HTTP资格重新验证，旧局部
   数值证据按适用范围复用，不冒称旧binary验收覆盖新身份。
3. 固定一次18请求诊断：同binary新服务off先跑1024/4096/8192
   各3次，再新服务on同序；每次输出256。每组payload0起点，组内
   不清cache，后续档继承先前状态，与旧完整矩阵前三档顺序一致。
   单流、C256/L2=16/mirrorK8/maxopen200、8192分块、容量262144，
   host/cache16GiB、swap0；MTP/Phase D关闭，chunk_order0。
4. 同时采集有读取窗口的全机/进程/driver内存元数据，明确别名、
   缺测和监测开销。仅证据足以证明同时点去重并集时评定54GB；
   否则记录严格可证上下界或UNKNOWN及缺口，不相加独立峰值。
5. 核验请求/阶段/形状/响应对应与正常清理，逐假设给出支持、排除
   （限本轮样本）或未决结论；更新状态/日志，分阶段提交推送。

## 预先提出的假设

- H1：prefill分区改变缓存末态，影响真正decode的补载，尤其前
  1/8/32步。仅末态不同不等于原因成立，需结合阶段读取与前缀变化。
- H2：旧dmiss/pmiss变化混入prefill中的singleton，误当请求阶段。
  用新边界差分量化，保留旧数据与原接受结论。
- H3：真实decode的物理读取或loader时间增加，即使总软件字节减少。
  PID全部文件、设备背景读取与重叠worker时间分别解释。
- H4：本机接口是否足以证明54GB同时点物理并集；NVML/NVIDIA进程
  计数、cgroup、RSS和页缓存均不能未经去重直接相加。

## 验证与停止规则

所有吞吐来自插桩诊断，不能翻转旧NO_GO，不能当统计非劣性或正式
性能验收。固定off→on顺序也不提供随机化因果证据。具体工具/实现
失败才修复并重验受影响项，不追加有利重复，不自动跑六档/生命周期
矩阵。模型/reference只读，原七处修改不进入隔离候选。

当前：阶段快照、HTTP ID关联/严格审计与独立RAM采样已成组实现，
完成作者、主控和交叉静态复核。错误路径会先等加载线程结束再读取
host元数据；实际GPU错误不把host缓存ID当device内容证明。阶段
capture_ns不包含锁等待、对象构造与末尾JSON输出，不能代表全部
插桩成本。decode路由序列未完整采集，缓存末态差异仍不是因果证明。

工具将插桩标记从旧性能接受流程中排除；审计核实际HTTP ID、完整
字段/层/缓存数组、运行日志激活、构建/工具身份及unit/进程组清理。
RAM collector明确保留未知，不把空nvmap/dma_buf当作CUDA为零。
实现与必要构建已经完成；以下为本轮实测结果。前述列表保留原冻结范围。

## 交付结果与身份

干净源码`0df355a434475a685ae62a5af9e9d234ca26fafc`，Thor Release、
g++14、CUDA 110a构建零警告；binary SHA256为
`1a83afcc853dafe5fe66f42af554cb8f6475a5a63969c2798c40a6f2ed61d399`。
第一项测试为真实HTTP固定11题（含200K），全部输出/长度同冻结参考，
11条真实request ID及阶段记录完整。随后六组受影响host/工具合同
139/139首次通过；最终18条诊断请求输出256、内容同参考，容量262144，
29条HTTP记录、源码/工具/配置、unit/runner进程组与清理审计通过。

新二进制的HTTP资格已实测；未重跑旧两项真实权重数值合同。本轮未改
GEMM/精度/分区计算，旧合同仅作为未改实现的有界历史证据，不是新
binary的整模型数值证明。未进行无插桩性能接受、完整六档或生命周期
矩阵；诊断完成不能翻转上一轮完整性能NO_GO。

### 两项首次失败与恢复

HTTP完成后的独立核验脚本曾未解包`read_phase_records`返回值，产生
TypeError。保留`quality-validation-first-failure.json`，只修正该
ignored脚本并重读已完成结果，无HTTP重跑或运行时改动。

原diag-off在启动服务前冷缓存门槛失败：四个safetensors各驻留4096B，
合计16384B；advice无错误、无dirty/writeback、零服务、零请求。
后续一次非触页mincore定位为文件内部页，排除尾页解释。可访问的
/proc扫描无引用，但有权限缺口；具体引用/回收原因UNKNOWN。
`DONTNEED`为非强制建议，零长度覆盖全文件，成功返回不保证零驻留。
参见[上游Linux 6.8实现](https://raw.githubusercontent.com/torvalds/linux/v6.8/mm/fadvise.c)
与[Linux手册](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html)；
这不冒充已核本机Tegra补丁，也未认定工具缺陷。

基于用户自主推进授权，在任何诊断推理前单独冻结环境恢复补充计划：
仅对四文件各再给一次只读advice，随后核全部226文件payload必须为0；
失败即止。实测恢复为0，再以新`*-recovery02`目录执行off/on各9条。
这是对原自定“失败即停”规则的显式补充，原失败与原计划未覆盖；
没有从性能结果中挑选重跑。新两组均再次通过原零缓存门槛，无进一步
恢复。补充计划、恢复、命令、身份、时间及18请求关联独立审计通过。
恢复调用次数由执行脚本/目标记录支持，未采syscall trace作独立证明。

## 真实阶段、形状与读取

下表每格为同档3次均值。PID字节为该进程全部文件的存储读取计数，
不是专家文件专属读取；GB为十进制。GPU loads为软件补载次数。
原始每次、前缀和互斥区间均保留在`analysis-result.json`，不删样本。

| 输入 | prefill loads off→on | 真decode loads off→on | 真decode PID GB off→on | PID变化 |
|---|---:|---:|---:|---:|
| 1024 | 12753.3→11072.0 | 6895.7→6942.3 | 20.539→23.086 | +12.40% |
| 4096 | 42995.7→26765.0 | 8125.3→8097.7 | 28.565→29.835 | +4.45% |
| 8192 | 71839.0→37589.0 | 8589.0→8318.3 | 31.099→30.277 | −2.64% |

H1仅支持“缓存状态不同与1K/4K额外decode读取同时出现”，因果未闭合。
九对prefill末尾GPU、L2、mirror集合均在全部48层不同，但on前1步、
前8步的GPU补载与PID读量在九对中全部更少；前32步GPU补载仅1K首发
增加，其余八对减少。因此样本排除“额外GPU补载集中在最初1/8步”
这一具体预测；不排除后续页缓存、路由需求或其他开销。

1K在第33–255步的on−off PID读量为+1.743/+2.111/+3.565GB；4K
第9–32步为+0.792/+0.419/+0.436GB，第33–255步仍+0.489/+0.929/
+0.986GB。不能把这些现象归结成“只恢复前几个decode步的热专家”
就能解决。1K GPU总补载三次差为+228/+60/−148，4K为+23/−66/−40，
说明更少的GPU补载也可同时出现更多PID读取。

初始状态也不完全受控：首发GPU集合/槽位/recency相同，L2集合相同，
但48层L2槽序与共同专家recency不同；后续请求继承两组各自历史。
固定off→on顺序、未穷尽采集decode router IDs、相同输出不等于相同
中间需求，这些因素使“缓存末态导致慢”的因果判断继续UNRESOLVED。

H2确认旧dmiss/pmiss分别按`T_sub==1`与`T_sub>1`形状归类，不能
直接当请求阶段。
1K两组prefill single-shape为0；4K每次off/on的singleton专家lookup
条目为20/30、miss为2/23；8K为0/20、miss为0/11–12。这里的计数
单位是专家lookup，不是token数或子块数。污染存在但量小，且1K没有
这种污染，所以不能单靠改标签解释本轮1K/4K实际读取增量。

H3确认1K/4K的每次真实decode PID读取均增加，loader stage与pread
累计用时也分别增加；8K三次读取、GPU总补载及这些计时均减少。
worker/stage/pread计时重叠，不能相加为关键路径。全请求PID均值
变化为+6.87%/−2.83%/−3.02%；软件补载下降不等于物理读取同比下降。

### 客户端与设备观测边界

| 输入 | 插桩TTFT均值 off→on（s） | 插桩decode均值 off→on（tok/s） |
|---|---:|---:|
| 1024 | 10.820→9.741 | 7.134→7.116 |
| 4096 | 25.842→19.562 | 6.550→6.513 |
| 8192 | 38.149→27.134 | 6.290→6.364 |

这些是描述性观测，不新增速度门槛或统计显著性结论；8K本次方向与
旧最小decode失败不同，不能用本次插桩结果覆写旧拒绝结果。
每请求快照capture总和中位数off/on为1.622/1.647ms，最大43.031/
40.186ms；未包括锁等待、对象构造与最终JSON输出，不能据此签插桩
无性能成本。JSON在计算结束后统一输出，客户端窗口仍需单列。

18条客户端进程窗口均取得PID/cgroup/模型所在分区的计数夹逼，
边界不确定量最大off/on 0.887/0.948s。设备读取量与PID量接近
（设备上界高出PID约0.000004%–0.104%），但设备含背景I/O，不能精确
归属于q4t；该窗口含客户端启动/退出，也不是服务器阶段的逐步定位。
cgroup I/O继续低计，off最后两条为0而PID仍约106GB；PSI与运行期
模型文件cache未观测。未把cgroup、PID、父盘/分区或软件字节相加。

## 整体RAM：可行性验证与未决项

恢复off/on的独立collector分别656/602样本，643/586个窗口绑定同一
活服务，9个请求标签完整；两组均STOP_FILE、rc0、摘要/身份/读取窗口
一致。首次冷态失败的8样本单列，零模型服务窗口，不混入模型RAM。
driver定时读取均成功，未采值保留NOT_SAMPLED；无上次值前向填充。

新确认的运行期覆盖缺口：nvmap始终0，DMA-BUF仅7个tegra_drm对象，
共67502080B；同期NVIDIA进程计数约56GB。因此这两个debugfs视图
不能提供本机CUDA分配的完整覆盖。空接口不能解释为CUDA占用为0。
官方接口语义参见[NVML查询文档](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html)、
[Linux DMA-BUF文档](https://docs.kernel.org/6.8/driver-api/dma-buf.html)
及[proc接口文档](https://docs.kernel.org/filesystems/proc.html)。

| 独立观测峰值（bytes） | off | on |
|---|---:|---:|
| NVIDIA进程计数 | 56237228032 | 55968792576 |
| RSS | 4679475200 | 4681834496 |
| 全机Linux nonfree | 81190055936 | 81167101952 |
| cgroup memory.peak | 17179885568 | 17179885568 |
| cgroup memory.current采样最大 | 17180045312 | 17180131328 |

独立峰值不能相加，也不能把NVIDIA峰差当省RAM。memory.peak比设置
16GiB高16384B；memory.current实测最大却分别高176128/262144B，
两个内核计数不同，不能只报较小的peak就宣称严格未超限。OOM、swap
均为0。全机计数包含其他服务和driver池，不是q4t独占物理量。

collector单窗口中位约0.67/0.65ms，最大118/129ms，总读取窗口
6.84/6.27s；约占观察时长1.04%，这不是测得的推理性能损耗比例。
没有GPU分配到物理页身份、CPU/GPU/未映射文件cache别名关系或原子
同时点；采样也无法证明未采瞬间峰值。严格服务物理上下界与并集峰
保持null/UNKNOWN，**54GB为INDETERMINATE**。本轮完成测量可行性
验证及现场采样，没有完成54GB资源验收。

## 下一项最小候选与推进队列

优先单独冻结“完整输入长度≤8192的请求沿用旧分块，较长请求使用
分区”的默认off候选。这是针对已拒绝短档的范围收缩提案，不是已经
证明的根因修复；不根据本次8K局部好转将其偷改为接受。

必须在完整tokenized输入已知时确定不可变request policy，经请求/
sequence和scheduler显式传到Model→Decoder→MoE。只看当前forward
的T≤8192会同时关掉长请求的8192块与短尾块；不能用进程环境变量或
thread_local跨请求切换。首版继续单流，混合policy批处理另立合同。
接入前复核数值/dispatch，干净构建后首测HTTP，再按冻结规则做完整
五档+目标容量接受；若失败保留结果并停止，不用本轮插桩资格代替。
下一轮还须明确8193–45055在本轮未覆盖，提前冻结阈值边界与长请求
之后短请求的缓存继承检查；短请求bypass不自动消除之前请求的历史。

暂不优先缓存末态恢复：legacy prefill末态是未观测反事实，恢复请求
入口不等于恢复它；host ID快照也不是payload，回写元数据会产生错误
权重。真正恢复需GPU payload/scales、LRU、L2/mirror及stream合同，
引入额外读取、搬运、TTFT与RAM成本。若下一候选仍失败，再另冻一次
针对页缓存/预读与真正decode路由需求的有界定位，不继续堆GPU缓存
启发式。RAM独立路线需匹配本机driver的分配/别名证据，不能靠现有
nvmap/DMA-BUF视图继续算术拼接。以上为后续提案，本轮未实现新策略。

## 证据索引

均相对证据根；摘要核对入口如下。原计划和首次失败均保留。

| 文件 | SHA256 |
|---|---|
| execution-plan.json | 0815b908cfb6433bc7875ca614a5dc284a3b6dae9b00c9a7fba112969ac729fd |
| cold-recovery-02/amendment.json | 4244c5c1ef655a6f021241fbd129078dd1676c6c79df0bb522bd91d79a14c3d0 |
| diagnostic-audit.json | 512f5941f8da49cb9d0bd2c66e3458d71bd44fba9467d42fbdef70df2b62aed2 |
| amendment-audit.json | 076ef4b23bb312f298aacc73bb776f123e3afcece2e2940d64f51b7d6524c301 |
| analysis-result.json | 4a6b14dbf72477ce8ec8d991475057e72f701fc0ef7e1a19157c92a6363a4b24 |
| raw-resource-audit.json | bfaa3ec4bd46ea923fdbd74097658dc829550d7f3546eb06476c35705f04af3e |
| resource-audit.json | caf171d29131e52ac0fa46bf86afdee463d77ad7b3cff3689919e64fafed5e99 |

`quality-decision.json`、`host-acceptance.json`、`result-summary.json`、
`resource-audit-interpretation.json`及`next-candidate-readonly-review.json`
补充逐次计数、采样成本、解释和代码位置。最终提交/远端/原七处修改/
模型元数据/二进制/服务清理身份以`delivery-audit.json`为准。
