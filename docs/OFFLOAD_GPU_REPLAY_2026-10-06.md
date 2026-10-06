# Offload GPU 缓存源码一致回放 — 基线复现，平票候选拒绝

更新：2026-10-06。承接16be72c，工作分支
`codex/offload-gpu-replay-20261006`，被测工具提交`15feb57`。
本阶段完成纯host离线研究：现行策略全部精确复现，唯一expert-ID平票
候选为`NO_GO_OFFLINE_WORK`，未准入runtime实现。无新增模型运行、HTTP、
GPU测试或bench；旧NO_GO、默认关闭和整体54GB未知保持。

## 核心结论

12个1K探针（k1–k3）在**固定实际阶段入口缓存、固定chunk/needed顺序**
下，潜在可避免GPU加载上界从入口distinct法的6036–6097次收紧为
35–47次，即实际prefill加载的0.286%–0.370%。大部分原宽余量由容量与
连续分块需求强制产生。此值不是已实现的减量或速度收益，也不约束改变
前驱/入口状态、分区、路由重排、kernel和lower-cache供给的优化。

唯一候选有9/16个prefill、14/16个decode阶段增加加载，未通过预先冻结
的逐请求不回退门槛。部分首请求的小幅减少不能抵消后续history劣化；
AS/CS的k3各增加886次decode加载。这是固定路由下的反事实工作量结果，
不是新测得的decode TPS回退。没有搜索第二个候选。

## 固定输入与精确基线

[范围计划](evidence/offload-gpu-replay-20261006/scope-plan.json) SHA256
`e294b25ea6f594bffea2de1aef35f17f80cca51f7949d9ef1affcaafcca43fd7`。
复用上一阶段m02-AS、m03-CS、m06-CL、m07-AL四个观测on服务，各4请求，
共16条完整路由。A/C为既有offload分区策略臂，S/L为1024/8193前驱，
k0为前驱、k1–k3各1024输入；输出均256。每层512专家、top10、256槽。
四个服务各自从观测k0入口初始化，之后连续推进4请求；不在后续请求或
阶段边界重置为观测缓存。AS/CS相同结果分列，不合并成独立重复样本。

基线精确匹配全部80个互不重叠区间（prefill、decode 0→1、1→8、
8→32、32→255）的10项GPU计数。每条请求入口及prefill/decode末态
共48个检查点、2304层状态的物理slot专家、raw tick、保护位、raw clock
全部匹配；12次跨请求衔接连续。前1/8/32步仅有计数，无逐层状态证据。
基线没有只拟合总加载量，也没有从后续观测状态纠正漂移。

运行时源码`6b51693`、binary SHA前缀`8447d898`保持原身份。源码约束：
PlanResolve先标完整needed，保护/reserved/仍needed槽不可驱逐；同次
planned重复命中会更新tick。空槽取最低物理slot，否则最低tick、同tick
最低物理slot。非空调用只加一次clock；每chunk提交完成后才规划下一块。
真实C++ lex std::sort同键顺序保留，不用Python稳定排序替代。
C的1K仍legacy，8193为8192行min_new加1行legacy；decode为singleton。
single/multi计数按chunk形状，单行prefill仍属真实prefill。

## 容量下界与逐请求结果

每层容量下界为首chunk相对实际phase入口的缺失量，加相邻needed集合
`P,N`的`max(0, |N−P|−(256−|P|))`。另算入口distinct需求下界。
组合下界为**各层两种下界取max再求和**；actual减组合下界是潜在可避免
加载上界，未证明所有下界可同时达到。本表基线下界使用观测phase入口；
结构化结果中的候选下界使用各自反事实phase入口。候选携带自己的状态，
后续入口改变，不能用基线的该上界约束候选的跨阶段收益。

表中Δ=候选−基线；负数为减少。上界列仅指上述固定实际入口/分块条件。

| 服务 | 位置 | 基线prefill loads | prefill可避免量上界 | Δprefill | 基线decode loads | Δdecode |
|---|---|---:|---:|---:|---:|---:|
| m02-as-on | k0 | 13705 | 54 | -5 | 7128 | +125 |
| m02-as-on | k1 | 12300 | 36 | +0 | 7000 | +253 |
| m02-as-on | k2 | 12255 | 38 | +45 | 6559 | +694 |
| m02-as-on | k3 | 12245 | 35 | +55 | 6367 | +886 |
| m03-cs-on | k0 | 13705 | 54 | -5 | 7128 | +125 |
| m03-cs-on | k1 | 12300 | 36 | +0 | 7000 | +253 |
| m03-cs-on | k2 | 12255 | 38 | +45 | 6559 | +694 |
| m03-cs-on | k3 | 12245 | 35 | +55 | 6367 | +886 |
| m06-cl-on | k0 | 37128 | 199 | +3 | 6752 | -56 |
| m06-cl-on | k1 | 12711 | 47 | -4 | 7129 | +124 |
| m06-cl-on | k2 | 12291 | 41 | +9 | 6798 | +455 |
| m06-cl-on | k3 | 12253 | 35 | +47 | 6469 | +784 |
| m07-al-on | k0 | 71471 | 520 | -6 | 7196 | -22 |
| m07-al-on | k1 | 12754 | 46 | -26 | 7227 | +26 |
| m07-al-on | k2 | 12273 | 42 | +27 | 6867 | +386 |
| m07-al-on | k3 | 12249 | 37 | +51 | 6481 | +772 |

8193前驱C/A的prefill上界分别199/520次（0.536%/0.728%）；1024前驱
k0为54次，不能混称所有1K均35–47。decode上界仍1995–3113次
（29.55%–43.07%），只是宽余量，未证明能够消除。所有32个phase的
入口/容量/组合下界和候选结果见[结构化结果](evidence/offload-gpu-replay-20261006/replay-independent-summary.json)。

## 候选合同与验证

只改occupied最老tick平票：从最低物理slot改为最低resident expert ID
（末位slot保持唯一）；空槽、容量、needed/reserved/protected保持。
候选从相同服务首入口开始，随后携带自己的反事实状态。路由、chunk摘要、
分块计数及resolve数与基线相同；两策略各12次history衔接检查通过。
冻结门槛为16条prefill各自loads不增、16条decode各自loads不增，且
至少一条prefill严格减少；前两项均失败，结论为有效否决，不是回放失败。

本机g++14/C++23 host构建零警告；核心27、分块helper11、runner17，
共55项合同首批全过，包括独立穷举下界和真实C++同键排序对照。
七个自有执行步骤（构建、输入提取、三组合同、两次实际回放）均首次
rc0、清理通过。静审发现的来源/候选前置缺口在首次测试前已修复，
历史审查记录保留。准备期只读路径查询错误也保留，不计作测试或回放失败。
没有因文档整理重跑已通过的合同或模型；旧HTTP/数值/质量证据只按原覆盖
范围复用，没有新增整模型精度或E2E性能接受结论。

独立结果审查直接核manifest的80区间计数与2304层入口/末态绑定，
重新核算两策略3072条层级上下界和32项候选phase门槛；未重放trace。
原始manifest、运行结果和失败记录保留在
`.q4t-work/offload-gpu-replay-20261006/`。工具与文档分阶段提交；最终
保护/独立审查摘要见[交付证据](evidence/offload-gpu-replay-20261006/delivery-evidence.json)。

保护范围为MAIN原七项修改、HEAD、diff与原binary身份，模型228项
路径/size/inode/device/mtime及config/index内容；metadata不等于权重
payload字节证明，不能排除瞬时写入。reference仅核tracked范围。
本阶段没有改动运行时代码，模型/reference保持只读操作。

## 下一步

本阶段以精确基线、容量约束定位和具体候选否决收束。当前证据不支持
继续无依据地换GPU平票规则。下一项研究应先选一个decode供给或未隐藏
等待的具体假设：已有长前驱后mirror命中减少、软件read增加的线索，
但逐miss lower-cache来源/victim/worker顺序以及SSD/H2D/host互斥等待
仍未知。先确定哪项新证据能区分假设，再冻结有界范围。

改变分区或入口状态的prefill方向仍开放，本报告不给额外速度百分比承诺。
任何runtime候选仍须另外冻结HTTP首测、数值、完整五档加目标容量/history/
质量/资源验收；本阶段条件未触发，未用离线计数替代这些验收。
