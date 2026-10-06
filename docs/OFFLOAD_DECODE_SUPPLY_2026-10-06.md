# Decode 缓存供给机制 — 直接竞争存在，主要缺口来自入口机会

更新：2026-10-06。本阶段完成离线分析、默认关闭的有界观测与固定
5服务27条HTTP验证。**同plan写回抢占确实发生，但不足以解释本批
长前驱后的主要mirror命中缺口。** 主比较k1的缺口468次，分解为各plan
入口候选机会差470次，加直接损失差−2次；这是实际执行的精确计数
恒等式，不是速度的独立因果份额。

离线工具为`8ea3d329a236e35dd0aba35ad619912e3e76cc28`，分支
`codex/offload-supply-20261006`。被测观测源码为
`75d26ff22391a4fd7549b1c39bf2b9c0f2cbbf89`，交付分支为
`codex/offload-supply-observer-20261006`；后续文档提交不改变运行时。
Thor Release构建零警告，binary SHA256为
`72a321a422facac605c13740ac8eb78e253056a6719c88bea71e3bbae9ef843f`。
没有实现优化策略，没有新的五档性能接受；旧NO_GO保持。

## 假设与冻结范围

[原计划](evidence/offload-supply-20261006/scope-plan.json)先复用原四个
on服务16请求，分析实际decode入口之后的255个forward、48层。
假设是：同一decode计划内，先完成的worker为GPU victim预留mirror
写回槽时，抢占另一个尚未claim的needed专家镜像，使后者转为软件READ。
抢占发生在reserve阶段，早于publish；GPU victim不在needed，并不
保证被覆盖的mirror entry不在needed。

scope SHA256为
`2bf0aa00fceebdc00d5b1e04ced774f3eb2ba4104d58e056a21d7c68205086e4`。
原证据没有逐miss来源与reserve/claim顺序，先做保守上界；只有不能排除
直接机制时才触发窄观测。[观测附录](evidence/offload-supply-20261006/observer-appendix.json)
固定A臂、5服务27请求，低于Goal原上限8服务32请求；没有追加有利样本。

## 离线结果：供给闭合，上界不能决定实际频次

逐层L2 clock的成功阶段增量等于L2 hits与read misses之和；全局L2hits
为零且各层非负，才可识别逐层实际read。用GPU loads减read得到mirror
hits。195840个decode层计划、768个分解、64窗口、16GPU末态全部闭合。
上界22项、分析器19项，共41项新合同首批通过；原55项GPU合同限定复用。

同plan直接损失上界为
`min(8, max(0,m−1), occupied victims, misses∩possible mirror IDs)`。
possible集合从实际decode入口开始，只加入此前plan的GPU victims；
不假设mirror是最近8次驱逐，不把上界当实际损失或历史反事实收益。

| 臂/位置 | mirror缺口 S−L | GPU loads L−S | read L−S | 长前驱直接损失上界 |
|---|---:|---:|---:|---:|
| A k1 | 470 | 227 | 697 | 1540 |
| C k1 | 452 | 129 | 581 | 1487 |
| A k2 | −272 | 308 | 36 | 1338 |
| C k2 | −215 | 239 | 24 | 1296 |
| A k3 | −96 | 114 | 18 | 1151 |
| C k3 | −93 | 102 | 9 | 1140 |

两臂k1上界均高于缺口，按冻结规则选择A臂观测。k1第32层缺口均为29，
上界仅7/9，该层不能全由当期直接竞争解释；剩余不自动归因于历史竞争。
12个k1–k3探针mirror skips为零，写回=驱逐=加载，长前驱写回反而更多。
“写回次数不足”不能解释本批命中减少。完整离线来源见
[独立摘要](evidence/offload-supply-20261006/supply-independent-summary.json)。

## 观测实现与真实环境验证

`Q4T_MOE_SUPPLY_OBSERVER`只接受未设置、0或1，默认关闭。开启时记录
实际L2/MIRROR/READ来源、入口needed镜像候选及reserve→claim→publication
关系，每层最多保存4条近期损失见证。plan及持久状态各≤4096B，新增JSON
≤1MiB/请求；诊断on实际最大158507B。关闭时没有观测状态分配、计数或
样本，也不输出新JSON字段；仍有分支检查及启动配置日志，不称零开销。

入口由单caller在前一worker barrier后、下一dispatch前复制，不新增锁。
跨worker事件使用原有`l2_mu`。没有新增CUDA调用、同步、copy或改变GPU
选槽、worker派发、缓存策略、GEMM、精度、预算。错误/部分计划保留失败
状态，不生成成功READ假见证。原静审发现与修复记录保留。

[执行计划](evidence/offload-supply-20261006/observer-execution-plan.json)
SHA256为`907111ee1b731521c286e90df3718457c11c25c340a3f66fabc90a03a7584b24`，
绑定312项来源；干净export有1621文件。顺序和结果如下：

| 顺序 | 工作 | 结果与覆盖 |
|---|---|---|
| 1 | observer on固定质量11题HTTP首测 | 11/11通过，含1K/4K/8K/45K/200K；输出文本和usage与冻结oracle相同 |
| 2 | 23项C++、23项Python新观测合同 | 首批46/46通过；C++含`-Wall -Wextra -Werror` |
| 3 | AS off、AS on、AL on、AL off各4请求 | 16/16通过；每条256输出，source/phase/trace/输出/清理闭合 |
| 4 | 资源、结果和独立审查 | 5服务、27HTTP、17客户端资源包络全部闭合 |

AS输入长度为`[1024,1024,1024,1024]`，AL为
`[8193,1024,1024,1024]`；k从0计。每服务前定向冷态，服务内保留缓存历史。
四诊断服务的旧phase/cache/timing/route bundle一致，仅新observer开关
不同。A策略位0/0/0，GPU容量256、L2容量16、mirror容量8、16 loader，
host/cache上限16GiB、swap0，单请求/单模型stream，MTP与Phase D关闭。

本Goal新合同总计87=41+46；已有55项GPU、8项资源算例只按固定身份复用，
不算本轮新增通过。没有新的模型数值微测或完整五档性能验收。质量通过
只覆盖固定11题，不外推整模型质量。构建、测试、HTTP与执行审计首批均
通过；首次静审问题保留，不因归档再次运行推理。

## 实测机制：22次直接损失，主要缺口是累计入口机会

8条observer-on诊断decode共97920个层计划：入口候选出现4482次，
mirror命中4460次，软件READ 50365次，GPU loads 54825次，L2hits为零。
**4482是每个plan入口机会的累计次数，不是一次请求入口的缓存容量，
也不是distinct专家数。** 22个直接损失全部有保留见证：claim时槽位
被同plan写回占用，实际成功READ，写回最终published。

| 序列/位置 | READ | mirror hits | 入口候选次数 | 同plan直接损失 |
|---|---:|---:|---:|---:|
| AS k0 | 6678 | 450 | 453 | 3 |
| AS k1 | 6056 | 944 | 950 | 6 |
| AS k2 | 5985 | 574 | 574 | 0 |
| AS k3 | 5967 | 400 | 401 | 1 |
| AL k0 | 6916 | 280 | 283 | 3 |
| AL k1 | 6751 | 476 | 480 | 4 |
| AL k2 | 6024 | 843 | 846 | 3 |
| AL k3 | 5988 | 493 | 495 | 2 |

22/50365=0.043681%只是这8条请求的软件READ事件频率。请求位置、事件
不是独立服务重复，比例不是全局反事实读量或速度收益上限。质量11题中
另有3次同类decode损失，单列保留，不并入诊断频率；全部19条on请求的
prefill均记录零次直接损失。

新on执行的S/L比较在总体和48层逐项满足两条恒等式：

```text
mirror缺口(S−L) = 入口候选差(S−L) + 直接损失差(L−S)
                  + 候选转L2差(L−S)
read差(L−S)     = GPU loads差(L−S) − L2hits差(L−S)
                  + mirror缺口(S−L)
```

| 位置 | mirror缺口 S−L | 入口候选差 S−L | 直接损失差 L−S | GPU loads差 L−S | READ差 L−S |
|---|---:|---:|---:|---:|---:|
| k0 | 170 | 170 | 0 | 68 | 238 |
| k1 | 468 | 470 | −2 | 227 | 695 |
| k2 | −269 | −272 | 3 | 308 | 39 |
| k3 | −93 | −94 | 1 | 114 | 21 |

候选转L2与L2hits差均为零。k0输入不同，仅保留描述；主比较k1及k2/k3
输入相同。k1 mirror由944降至476，`468=470+(4−6)`；直接竞争不能
解释主要缺口。入口机会取决于此前缓存、写回及调度历史，470和−2不能
当成相互独立的因果份额。k2/k3方向反转完整保留，不作为热身删除。

原16条离线请求与新请求不池化；旧上界不能移植到新入口历史。新observer
可能扰动worker顺序，不能从新频率推断旧请求频率。8对off/on的GPU loads
相同，软件READ仍有小幅差异；速度变化方向混合，只作单cell描述，不能
声称纯观测开销、稳定回退幅度或提速。全16行指标、22见证与对比见
[交付摘要](evidence/offload-supply-20261006/observer-delivery-summary.json)。

## 等待与资源的证明边界

true outer T=1路径在plan前同步单一模型stream，并经过前一worker
barrier；当前GPU victim不在needed，所需mirror来自plan入口。这一
源码结论不能泛化到大prefill内singleton子块，也不能把lazy event标志
当作未完成拷贝的证据。累计/并行/嵌套计时不能相加为独占SSD/H2D/host等待。

资源审计覆盖2368样本、27HTTP时间窗、17客户端I/O包络；质量11题共享
一个包络，没有逐题存储归因。2140次GPU计划跳采、各服务15次启动前
cgroup未知及全部5服务PSI未知均保留。PID存储read_bytes、逻辑rchar、
cgroup和设备夹逼计数分列，不把不一致归一化，也不当作专家payload字节。

memory.current采样最高超16GiB设置233472B，终态charge peak最高超
73728B，二者分列；`memory.events.max`非零。观测OOM/swap为零不等于
无压力。NVIDIA/cgroup/PID/file-cache存在重叠和未知，整体物理54GB仍为
`INDETERMINATE`。117MB原资源报告保留本地，归档仅放经过核对的紧凑摘要。

## 阶段决定与下一方向

决定为`DIRECT_SAME_PLAN_LOSS_OBSERVED_FUTURE_GUARD_CANDIDATE_ONLY`。
按预先规则最多提名一项未来候选：写回选槽时跳过同plan尚未claim的
needed入口mirror。它**尚未实现、未准入性能验收**；改变保留策略会改变
后续历史，当前直接频率不能给它设全局收益上限。

本批直接损失少且不能解释主缺口，因此不把该guard当作主要提速方案。
下一研究优先追踪mirror保留与替换历史：哪些入口机会消失，何时被覆盖，
是否在稍后再次needed，以及能否形成可实现的保留策略。这是后续研究问题，
不是本阶段新增第二项优化。另冻范围后才实施；任何提速改动仍须HTTP
首测及完整五档/目标/history/资源验收。当前不承诺prefill提升或decode提速。

## 保护与交付

[独立结果审查](evidence/offload-supply-20261006/observer-result-independent-review.json)
通过：27原始phase对象、1824个phase-layer差分、4总体/192逐层比较及
22见证均复核。原分析SHA256为
`b94ea7185510563a29497d53933afb3487d0246a83d0c539d3514dac795ce73a`；
独立审查SHA256为
`58e8d71304b6f1d64d68c6862eee5282b1c5226e0eaca1979ba4f94e99a1e05a`。

最终保护首次PASS，检查自身退出码0、清理完成。MAIN原HEAD、七项修改的
完整diff及binary保持；离线W仍为干净8ea3d32，被测O为75d26ff。模型
228项路径元数据及config/index摘要保持；审计不读权重payload，元数据
相同不证明内容逐字节相同或从未发生瞬时写入。reference证明限定tracked
Git状态；自有进程清理不证明全机GPU空闲。原件与归档映射见
[证据索引](evidence/offload-supply-20261006/README.md)。

最终文档交付在以上运行时身份之后，只改变docs；提交推送及远端身份由
本地`.q4t-work/offload-supply-20261006/observer-delivery-independent-review.json`
另行记录，避免在提交内自引用最终commit。文档归档不重复HTTP、trace解析
或资源原始流审计，不把Git推送当作性能通过。
