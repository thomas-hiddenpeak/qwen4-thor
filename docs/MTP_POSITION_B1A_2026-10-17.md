# MTP 精确位置下传 B1a

日期：2026-10-17。状态：实现与有界验证完成；性能证据不足，候选不合入 main。

## 冻结范围与身份

起点 main `867b8a6`；推理算法基线 `28a3783`，保留当前 binary
`8685d8bf4068b81df0f5162d6c55924d93eae793d17664a1b465ca7587d6c6ee`
至 `.q4t-work/b1a-20261017/q4t-baseline`。既有接受 binary
`be74946d8159…`及原始证据不修改。当前无有效工作分支，创建唯一
`codex/mtp-position-b1a`用于发布未决候选，避免将未验收实现直接推入main。

通过 ARCHIVED_RESEARCH 索引读取固定归档 `9a5bc0c` 的综合计划B1a。
只改 MtpForward 可选末参（默认-1）及 MtpSpeculativeStepMulti 草稿循环、
extend 两处调用。max来自同一上传数组，保留位置复制、实际T、分派、QSA、
接受/恢复、KV更新、输出和空extend跳过；不改初始化、旧single API、
主模型、scratch、GPU接受元数据、Graph或head。不移植外部代码。

收益类型：消除每次迁移调用的一次重复位置D2H、host临时vector和stream
等待；k3非空轮通常3次，k1非空轮1次。不声称整步无同步；MoE计数仍回读。
此为执行简化候选，不要求最低提速，不用同步次数推算耗时收益。

## 执行前判定卡

- 候选源码diff、binary/test SHA、构建cache、模型config/index SHA、工具
  SHA、环境记录在 `.q4t-work/b1a-20261017/`，运行前冻结。
- Release、g++14、CUDA13.3、SM110a；构建零警告。
- 首测真实HTTP：候选 quality-on 11题，然后on五档×3、quality-off 11题、
  off五档×3；共52个生成请求。默认MTP k3和普通decode独立组。
  固定容量1/8192/208896，greedy，原snap-records输入，每档输出256。
- 输出/提示参考：`.q4t-work/mtp-numerical-closure-20261016/http/`
  对应quality-on、on、quality-off、off；完整原始数据可用才运行。
  旧计时只作历史观测，不能冒称当前binary或当前环境的实测。
- HTTP完整性/质量/长度/确定性/参考输出通过后做直接合同。
  新增 `mtp_position_metadata_exact`：实际MtpForward，-1设备回读与精确
  host值同形逐位比较sample/multi/logits、全部draft KV/raw/comp；
  位置3/4/8191/8192/8194/8195/8196，T=1…8，非单调slot，连续两次前向。
  另覆盖生产multi k1/3、接受数和终止边界，记录实际覆盖而非泛化保证。
- HTTP尾部/取消使用既有boundaries驱动：6个生成、1个取消中的生成、
  1个取消控制请求，合计额外8请求。总HTTP预算60；无连接探测。
- 性能按各档TTFT均值、总decode tokens/总decode时间分别报告，正差表示
  变慢。历史三次范围仅描述。所有10个指标分辨带与可信差值不确定区间
  **未建立**，不填任意百分比；因此除独立真实退化证据外，性能出口为
  **证据不足**，不能因输出相同或区间重叠宣布持平，也不能凭单次方向
  判NO_GO。MTP与普通decode共20个指标分别记录。
- 有界诊断：最多一次离线源码/日志审查，0新增HTTP请求，最多两个机制
  假设（位置不匹配导致分派变化；环境/测量不可比）。没有可区分证据即
  停止，禁止为建立有利带宽追加采样或启动通用profile研发。
- 全阶段由同一个 gpu_exclusive guard 持锁；竞争/监控失败ENV_ABORT，
  保留现场，不视作候选失败。失败不覆盖，具体修复后仅重验受影响项。

## 可复用证据及边界

PLE上传所有权4项、独立accepted-prefix/checkpoint/prefill五项与完整
256步N=4近tie结论来源 MTP_NUMERICAL_CLOSURE phase02/03；对应实现、
模型与测试依赖未改，复用其局部机制结论。新MTP位置接入仍需新直接合同
与HTTP，不能把旧完整模型结果登记为候选binary实测。无新增多流发布承诺。

## 结果

候选binary：`44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03`。
构建与最终直接合同构建均零警告。GPU guard为COMPLETED、child=0，
未检测到竞争，所有子驱动及服务器正常退出。轮询仍不能排除短暂不可见负载。

| 验收层 | 结果与范围 |
|---|---|
| HTTP质量 | on/off各11/11；提示与已接受参考输出一致，包含204800-token题 |
| HTTP五档 | on/off各5×3；全部成功、实际长度合规、256输出、length结束、重复确定性及参考文本一致 |
| 位置合同 | 56组同形状×2次连续前向；sample/multi/logits有限且逐位相同，全部draft KV/raw/comp逐位相同 |
| 生产multi合同 | 9/9直接测试、0失败/跳过；k1/3、非单调slot、不同P、a=0/1/3、空/混合终止、非法输入拒绝 |
| 尾部/取消 | 6个尾部/恢复请求、取消中的生成及取消控制，共8请求通过 |
| 性能接受 | **证据不足（INSUFFICIENT_EVIDENCE）**；不是GO，也没有证据确认代码造成真实回退 |

MTP/普通decode分别如下。TTFT为三次均值；decode为总255×3 token / 总
首末token间隔（客户端估计）。历史列来自be74946d的2026-10-16冻结结果，
不是当前对照binary的新实测；跨日环境可比性与可信分辨带仍不足。

| 模式 | 档位 | 历史TTFT s | 候选TTFT s | 历史decode tok/s | 候选decode tok/s |
|---|---:|---:|---:|---:|---:|
| on | 1024 | 0.955 | 0.961 | 34.523 | 34.707 |
| on | 4096 | 2.929 | 2.940 | 35.464 | 35.622 |
| on | 8192 | 5.619 | 5.627 | 37.385 | 37.481 |
| on | 45056 | 31.898 | 31.940 | 35.887 | 36.022 |
| on | 204800 | 166.968 | 166.949 | 33.168 | 33.281 |
| off | 1024 | 0.785 | 0.790 | 18.639 | 18.615 |
| off | 4096 | 2.611 | 2.618 | 17.878 | 17.854 |
| off | 8192 | 5.065 | 5.085 | 18.141 | 18.084 |
| off | 45056 | 30.311 | 30.341 | 17.874 | 17.848 |
| off | 204800 | 158.890 | 159.156 | 17.145 | 17.104 |

各档逐次TTFT/decode、实际输入输出数、整请求latency及首次加载后请求
标记见[summary.json](evidence/mtp-position-b1a-20261017/summary.json)。
每个模式仅1K第一条是服务加载后的首请求。SSE可能批量输出，不能给出
准确token ITL分位数。MTP decode历史对照观测约+0.26%～+0.53%，off约
−0.13%～−0.31%；这些数字不构成收益或回退判决，TTFT亦逐档保留。

唯一离线诊断审查：位置错误/分派改变假设由实际数组取max的源码、边界
逐位比较及HTTP相同输出共同检查；未发现该机制。环境/测量可比性没有
足够证据形成分辨带，不把未决差异称为噪声。0新增诊断请求，按冻结出口
停止；不追加profile、调参或同类采样。全部60个HTTP请求预算已执行。

保持独立accepted-prefix、PLE所有权与N=4的既有复用范围，不将本次
2层受控multi测试当作全模型任意多流证明；a=2未作为生产step独立控制
场景，T=3等实际extend形状已在位置合同覆盖。未修改数值阈值。

[证据目录](evidence/mtp-position-b1a-20261017/README.md)保存摘要、身份、
驱动和日志；原始DB、请求/响应、binary及监控保留
`.q4t-work/b1a-20261017/`，raw-manifest逐文件记录SHA。
候选保留工作分支以供审查/回退；生产build/q4t仍为8685d8bf，main未改。
回退本实现仅撤销MtpForward末参/两处下传及相关测试，无需回退PLE修复。


直接合同执行前补充：精确名称见本地contracts.required（9项），包括
原multi自然k3及k1、全终止k1/k3、混合终止、无效输入拒绝。测试专用
源码在HTTP运行期间完成构建，生产候选binary未变；最终测试身份单独
记录于contracts-identity.json。metadata共56组×2次连续前向×2种参数，
同形状态/输出逐位比较，覆盖extend的1…8行；不把人工稀疏位置当完整
长上下文质量证明。

直接合同最终夹具增加可控零head的k3全接受及构造首个接受草稿的k3
场景；只在测试内改变加载的权重，不触及只读模型文件。最终构建零警告，
身份见contracts-identity-final.json，均在直接测试执行前冻结。HTTP候选
未变。后续仅整理生产同步注释，执行代码仍与已构建候选相同。

直接测试前审查进一步固定mixed-terminal：终止逻辑位置较大的序列，
只剩较小位置进入extend，并明确断言另一序列不终止。此测试变更在
全部HTTP性能请求结束后才编译，不在计时采样期间运行构建。执行身份
以contracts-identity-executed.json为准；前三份身份保留准备过程。

交付审查仅整理测试成员命名、include和slot注释，重新构建零警告；
最终测试binary `85ab546b81e56c1715c9c63e6a9cbf95f714d8fa658cf9687d578844c812052c`
与执行九项合同时逐字节同一SHA。见delivery-identity.json，复用9/9，
不重复GPU测试；生产保留binary亦再次核验未变。
