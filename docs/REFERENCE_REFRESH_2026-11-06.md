# 全部参考项目增量审查与首轮改进

状态：目标已建立；来源清单冻结，两个工作包进入开发，均未验收。
检索日期：2026-11-06 CST。上游可继续变化；本文的“当前”
仅指[来源清单](evidence/reference-refresh-20261106/upstream.json)中的精确提交。

## 范围与交付规则

覆盖 `reference/`、REFERENCE.md 和历史研究中登记的20个GitHub项目，
另有TileKernels包及模型技术报告。TileKernels的PyPI元数据还确认了
其官方GitHub入口。本轮未更新只读reference、安装或执行外部项目。
所有默认分支均已查询成功，5个与旧pin相同，15个有更新；不将这一
盘点称为上游全部提交的逐行审计。大项目compare返回至多300文件、
100提交，另取最近12提交；重点路径补查15提交及固定版本源文件。

接受基线为main `def8cc96`，运行来源 `95f86318`、默认程序 `c1bdb0a7`
与sidecar `0efc2c0d`，协议 `9c8758c4`。本轮复用
`codex/mtp-selected-head-rows`，旧GDN NO_GO实现保留在历史 `f9488f70`；
开发新工作包前恢复其3个运行文件到main，避免混入失败候选。

继续沿用用户“不追加测试”：当前仅研究、静态审查、开发与验收准备。
不执行单测、HTTP、模型、GPU、bench或新CI，不替换默认。阶段提交
显式skip CI；开发完成不等于接受或发布。用户负责的长稳不纳入首轮。

## 全量版本与增量矩阵

短SHA的完整值、查询结果、发布元数据、源文件URL/SHA256均在来源清单。
下表发布栏是GitHub latest接口返回值，**不是默认分支的发布资格**。
vLLM标签带rc3但API标记prerelease=false，不能据此称为稳定版。

| 参考项目 | 本次head / latest发布 | 增量与本项目取舍 |
|---|---|---|
| [sglang-ssd-stream](https://github.com/garnermccloud/sglang-ssd-stream) | `155a9496` / v0.3.0 | 无新提交；PLE流式读取已采用，独立GPU的CPU专家卸载不能搬到Thor当容量卸载 |
| [vLLM](https://github.com/vllm-project/vllm) | `bc9a3ac5` / v0.33.0rc3 | QSA投影/prepare融合、跨层top-k缓冲复用、SM110 cooperative top-k修复、FP8 KV scale修复、PLE磁盘卸载和长prefill HC融合；逐项判别，不能只看release摘要 |
| [SGLang](https://github.com/sgl-project/sglang) | `a99c1a85` / v0.5.23 | QSA/PLE调用者持有输出、recurrent/KV统一所有权、loader释放临时参数；本项目已有工作区及完整状态保存，CP/PD不直接移植；旧pin与main分叉，不能当线性升级 |
| [Transformers](https://github.com/huggingface/transformers) | `cdb75de4` / v5.22.1 | `indexed_attention`命名、indexer位置缓存生命周期、left-padding eager NaN修复；发现本项目层类型解析缺口，进入R1；参考环境5.16.1保持 |
| [DwarfStar/ds4](https://github.com/antirez/ds4) | `b3f50180` / 无release | 最近Qwen checkpoint v2、SSD与驻留算术一致、Metal Q2对齐及prefill维护；CUDA路径FP16 KV/softmax与本项目BF16数值不同，不整套移植 |
| [TokenSpeed](https://github.com/lightseekorg/tokenspeed) | `23791cea` / v0.1.1 | QSA streaming exact-refinement及共享K tile；SM107 TMA-gather prefill，输出/同分ID顺序不保证，不能替换本项目确定性选择合同；见R2 |
| [FlashInfer](https://github.com/flashinfer-ai/flashinfer) | `096ca394` / v0.7.3 | cuDNN QSA前向、实验b12x QSA、GDN/causal-conv及可审计tactic；直接QSA接口有架构/布局限制，实验目录不视为已成熟支持 |
| [FlashAttention](https://github.com/Dao-AILab/flash-attention) | `34a06b99` / v2.8.3.post2 | FA4显式调优、hd256 paged/varlen维护；11.x分派存在不等于本项目逐token稀疏名单可直接输入，见R2 |
| [qwen35-thor](https://github.com/thomas-hiddenpeak/qwen35-thor) | `57e29777` / v3.1.0 | 无新增；硬件/引擎组织参考，模型与数值路线不同 |
| [Qwen3x-Orin](https://github.com/thomas-hiddenpeak/Qwen3x-Orin) | `7aa0dd2b` / 无release | 新增精确NVFP4分解评估与NO_GO；无已接受的新投影可直接采用。tokenizer参考保留，不引入其治理体系 |
| [thor-probe](https://github.com/thomas-hiddenpeak/thor-probe) | `48166851` / v1.0.0 | 无新增；不重跑硬件探测 |
| [thor-bench](https://github.com/thomas-hiddenpeak/thor-bench) | `3a33a905` / 无release | 无新增；历史数据只读，不执行bench，不覆盖本项目后续实机指令证据 |
| [KVMem](https://github.com/kvmem/kvmem) | `a2d8d9bf` / v0.19.1 | checkpoint覆盖/预算、fork源保护、LoRA接入；本项目已交付SSD完整会话与前缀种子，任意中断/跨进程持久恢复仍另立需求 |
| [NInfer](https://github.com/Neroued/ninfer) | `a754fcf3` / 无release | context压力下防饥饿、清理后发布terminal、缓存回收/监控细化；与本项目已验客户端准入公平不是同一合同，不追认GPU时间或内存压力公平 |
| [Strata](https://github.com/Niko1221/Strata) | `9f7d977d` / v0.2.11 | UMA首次运行专家缓存策略和多后端QSA；host/pinned仍计Thor物理RAM。suffix及完整状态已有交付，量化KV/FP16路径不混入 |
| [kvmem-actkv](https://github.com/melis2023/kvmem-actkv) | `fe4c5d9d` / 无release | 无新增；近似淘汰仍不符合当前完整候选历史合同 |
| [actkv-open](https://github.com/Maurya30/actkv-open) | `2f832f7e` / 无release | 重复eviction保护prompt、评估按对应FullKV预算/配置计算；非官方且仍删除KV，不据此恢复近似淘汰路线 |
| [Megatron-LM](https://github.com/NVIDIA/Megatron-LM) | `9cfb771d` / core_v0.20.0 | token级snapshot、prefix-cache拆分、batch-invariant sigmoid路由及专家权重布局复用；本项目softmax router、单Thor及请求边界快照合同不同 |
| [DeepInfer](https://github.com/deepseek-ai/DeepInfer) | `a679bb86` / 无release | 仅3新提交：严格布尔env、DeepEP pin、部署QA。布尔显式解析值得借鉴，不能批量改本项目原本按presence定义的诊断开关 |
| [DeepEP](https://github.com/deepseek-ai/DeepEP) | `f6aa38fe` / v1.2.1 | 仅2新提交：HTTPS submodule及格式流程；未发现新的Engram传输算子增量，单GPU不导入分布式依赖 |

TileKernels仍为[PyPI 2.1.0](https://pypi.org/project/tile-kernels/)，官方仓库
head固定在来源清单；不安装、不import，也不把仓库head当已安装wheel。
模型论文[arXiv:2608.30320v1](https://arxiv.org/abs/2608.30320v1)的提交历史
仍只有2026-08-31 v1。本地PDF首页为08-26；已登记本地SHA，但未声称
与arXiv PDF字节相同。架构思想不改变当前checkpoint合同。

## R1：层类型兼容及明确拒绝

类型：兼容性/输入正确性修复，不是性能优化。

[HF当前配置源码][hf-config]将旧checkpoint的`full_attention`映射为
`indexed_attention`；5.16.1包此前使用`qwen_sparse_attention`。
本项目 `src/io/model_config.cpp` 只把字符串`full_attention`计为QSA，
其余字符串未经校验。全部改名时会报“没有full attention”；混合配置
只要还留一个旧名字就可能通过解析，未知层随后会落入GDN分支。
非字符串还会先被丢弃，错误位置不可见。

限定改动：只在`qwen4_exp`的`text_config.layer_types`入口，将三个
已知QSA名字统一到内部`full_attention`；保留`linear_attention`，
拒绝未知名字、非字符串、非数组、空/错长度。失败不发布部分配置。
不推测缺失列表，不改变权重、层顺序、MTP配置、多模态或其他模型支持。

准备CPU用例：三个别名与混合列表得到相同QSA路由；合法列表不重排；
混入未知类型、非字符串、缺失/错误列表、长度错误均定位拒绝；真实
模型配置沿用已有测试。恢复测试后先跑这些缺陷用例，再做适用HTTP
质量与工具闭环；旧配置的推理路径应保持，但未测不能标为通过。

## R2：QSA prefill实现选择前补足归因

类型：耗时归因准备；**当前不声称性能优化成功，也不移植新内核**。

既有45K细计时QSA core约9.20秒，包含主Q/K准备、indexer投影/准备、
评分/选择和attention；不能把全部时间算给最后的稀疏attention。
当前已具有GQA复用、BF16 MMA、PV寄存器累加、cp.async双缓冲和shared
padding，不能再把这些列为新增优化。旧五块评分优化也已在主线。

上游候选的实际边界：

- [vLLM合并投影#59533](https://github.com/vllm-project/vllm/pull/59533)
  的1.45–1.68倍来自部分GB300投影形状，M4096接近持平；其报告单并发
  E2E吞吐为−0.4%，不是本机收益。主QKV约0.30秒，indexer投影位于
  core内尚未分开，不能把0.30秒当全部可融合投影成本。
- [vLLM prepare融合#57097](https://github.com/vllm-project/vllm/pull/57097)
  移除launch与临时读写；本机未来实现必须保留norm输出的BF16舍入、
  M-RoPE位置、raw/完整压缩组状态及跨kernel可见性，不能仅按公式融合。
- [FlashInfer QSA#7532](https://github.com/flashinfer-ai/flashinfer/pull/7532)
  bitmap为SM100/103/107，indexed为SM100/103且12Q/1KV，后者文档明确
  非逐位重复；本机24Q/2KV、SM110和分页状态不能直接适配宣称通过。
- [FA4入口][fa-interface]包含11.x与hd256分派，但token gather要求qv
  （MLA），且拒绝paged KV与top-k gather组合；block-sparse适配仍需
  证明逐query四token微块、因果尾、顺序和临时内存，不能当现成QSA。
- [TokenSpeed说明][ts-qsa]的SM107 TMA-gather不自动适用Thor；stream
  selector不承诺同分ID及输出顺序，可能影响本项目后续softmax舍入。
- vLLM SM110 cooperative top-k修复提醒核对真实occupancy，本项目
  源码未使用cooperative launch；跨层top-k缓冲也已由scratch复用。
  FP8 KV scale修复不属于当前BF16 KV默认路径。

开发内容限定为可选的QSA细分计时：主prepare、indexer投影、indexer
prepare、score/select、sparse attention五段。复用现有layer计时机制，
新增显式开关，仅large-T prefill记录；不开启时保持原计时事件集合。
报告嵌套层次/完整性，不能把子项加到总耗时上，也不能把事件区间
视为纯kernel或无观察扰动的真实E2E。

后续获准测试时：先正常HTTP确认候选身份/输出，再在同租约内有界
采集该开关与关闭对照。只有找到可观成本且存在保合同机制，才冻结
一个性能候选和收益门；其首测仍HTTP、之后才适用五档/数值验收。
不为了本轮“有代码”盲改投影、top-k或attention算法。

## 本轮暂缓与回退

其余参考项目有可借鉴能力，但本次不扩展CPU容量卸载、近似KV删除、
分布式PD/EP/CP、任意token快照、LoRA、新客户端工业化或GPU时间公平。
已验完整状态恢复覆盖RoPE/raw/压缩组；HF缓存生命周期修复用来复核
状态合同，不以名称相似宣布本项目也存在同一缺陷或已全量证明无缺陷。

两个工作包分别以提交为回退边界；不回写模型/reference，不覆写接受
元数据。未构建/未验时只交付源码准备，默认pair与已验主线继续可用。
具体完成身份、构建及测试状态另记阶段结果，不能由本计划推导通过。

[hf-config]: https://github.com/huggingface/transformers/blob/cdb75de4adf71059c01f58dbd2d24d85084a397d/src/transformers/models/qwen4_exp/configuration_qwen4_exp.py
[fa-interface]: https://github.com/Dao-AILab/flash-attention/blob/34a06b99d60ea26bfce91432c9fbb6de1a1b94ec/flash_attn/cute/interface.py
[ts-qsa]: https://github.com/lightseekorg/tokenspeed/blob/23791cea04c42d5b6c50980914ccca5a5c4eeb9e/tokenspeed-kernel/python/tokenspeed_kernel/ops/attention/qsa/README.md
