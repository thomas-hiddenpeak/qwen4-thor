# GDN qkv/z BF16转换复用：首轮有效，未达收益门槛

2026-11-06。按[事前判定卡](GDN_BF16_INPUT_PLAN_2026-11-06.md)完成限定
实现、零警告构建及HTTP首验。结论为**NO_GO_FEASIBILITY_TARGET**：
四格解码变化−0.330%至+0.126%，均未达到各自≥3%的继续投入门槛。
保留当前默认，停止本轮，不补样、不进入完整数值/五档验收。

## 实现与身份

- 研究源提交`39d69b8f6b84cf8f75923955c9305691bc018faf`，继续使用
  `codex/mtp-selected-head-rows`。旧末行head运行文件与对应来源清单
  已恢复main；旧原型完整保留于`64ee7ce6`及此前提交。
- 相对接受版本223个运行来源，仅`gemv.h`、`small_m_gemm.cu`、
  `linear_attention.cu`三个文件变化，不包含旧head或客户端改动。
- 候选只在主模型一条序列的checkpointed T=4、K=2560、qkv N=10240/
  z N=6144投影启用，无FP8 shadow、无投影ablation、非ragged；普通
  prefill/decode、其他投影、状态和head保持。合条件launch失败直接报错。
- 每CTA精确转换BF16输入为FP32 shared并按[row,pair,K/8]布置；保持
  原lane/K遍历、f32x2 FMA次序、warp归约、alpha=1与BF16 RN舍入。
  这是源码合同审查，**不等于已完成独立逐位数值验收**。
- 候选ELF SHA256：
  `7a513d5f783966170978e2075a007b977a8904076889b3b5411e65fa4da0f9e2`。
  接受默认仍为`c1bdb0a7`，sidecar`0efc2c0d`，主线`def8cc96`保持。

## HTTP实测与判定

顺序full父→full候选→subset候选→subset父，每臂一次启动。每臂1K与
45K各3请求、256输出，另11个五档质量题、最多32输出；实际性能输入
998与45044 token。max_seq1/prefill8192/max_len208896、MTP k3，
startup-plan off，suffix/cache/计时器关闭，无warmup、生成探针或重试。

| head / 上下文 | 父decode tok/s | 候选decode tok/s | decode变化 | E2E总时间变化 |
|---|---:|---:|---:|---:|
| full / 1K | 35.556685 | 35.572830 | +0.0454% | +0.0718% |
| full / 45K | 35.915215 | 35.960357 | +0.1257% | +0.0694% |
| subset / 1K | 38.715175 | 38.587581 | −0.3296% | +0.3807% |
| subset / 45K | 39.433197 | 39.479864 | +0.1183% | −0.0610% |

decode为`sum(output_tokens-1)/sum(latency-ttft)`；E2E为同档三请求
latency合计，正值表示耗时增加。两head各两档均要求decode≥+3%且
E2E下降，四格全部未达标。首请求和逐请求TTFT/decode/E2E原值完整
保存在[决策原值](evidence/gdn-bf16-input-20261106/decision.json)。

每身份/head只有一次启动；跨head的AB/BA不是同head的平衡重复。
这些是描述性观察，不能据此声称统计退化、精确持平或统计非劣；
工程结论是未达到事前继续投入目标，新增复杂度没有获得可用收益。

68个响应全部通过；同head父候选的prompt/文本/输入输出计数/finish
一致，按请求ID绑定的接受直方图也一致；44个固定质量期望通过。
直方图不代表逐轮接受顺序记录，HTTP输出也不替代完整logits/状态的
逐位合同。收益门未过，因此独立数值、多槽/缓存及完整五档B阶段未触发。

## 代价与可解释边界

结束后仅离线读取ELF编译资源：M=4原特化40寄存器，候选54；动态
shared由20KiB增至40KiB，静态shared均1024字节，stack/local均0。
没有spill证据，也没有实际occupancy或带宽计数器测量。

候选只减少重复输入转换，没有减少BF16权重的逻辑遍历量：36层每轮
约3,019,898,880字节（2.8125GiB），不等于实测DRAM流量。增加的
shared访问和资源占用可能抵消转换节省，但本轮没有做确定因果归因。
结论仅针对这一实现；没有运行bench、追加profile或搜索其他参数。

## 有效性、失败保留与清理

- 4启动/68HTTP，阶段1680.229秒，租约1681.085秒，均在2400秒预算内。
- 公共监控1681完整采样、53来源；3项PSI预先登记不可用。退出pending0，
  退出后新采样、最终join及postflight有效。1541次guard monitor、
  1542次NVML查询无观察到的竞争；轮询不能排除短于采样间隔的干扰。
- 独立脚本从12个SQLite原始响应重建全部输出/时间/质量及四格判定；
  4548冻结输入、218封存原件、197模型文件元数据核对。原模型目录
  只读，本轮没有重新计算全部模型权重内容摘要。
- 离线审计测试首跑7项通过，1项夹具把有效7秒区间误列为零区间；
  将TTFT从1改为与latency相同的8后，仅重验该项通过。首次失败日志
  与修复记录均保留，分析器、候选和实测输入未改变，没有重新推理。
- serve清单38字段/10路由/112来源锚点及3个变异检查通过；该结果不
  认证新CUDA数值。源提交的[公共host检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/42107326430)
  通过，完整验收没有因此被替代。
- 21个已观察自有进程身份均退出，8762可绑定、GPU计算上下文为空，
  原共享锁dev66305/inode3294067已释放。未干预其他进程。
  默认程序及sidecar全文摘要不变，未部署候选或合入主线。

## 证据与交付边界

紧凑证据见[索引](evidence/gdn-bf16-input-20261106/index.json)、
[独立复核](evidence/gdn-bf16-input-20261106/independent-review.json)及
[清理确认](evidence/gdn-bf16-input-20261106/cleanup.json)。完整本机记录：

`/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-selected-head-rows-20261106/source/.q4t-work/gdn-bf16-input-20261106/`

其中`plan.json`冻结输入，`run/`为原始响应与监控，`decision.json`为
投入判定，`evidence.tar`按SHA保存原件与输入并回读核验。归档包含
独立subset head产物，不包含原模型目录内容；原绝对路径保存在catalog。
后续源码变化时应按catalog恢复冻结身份，不能用新HEAD冒充本次实测。

本次交付是可回溯的研究实现与NO_GO决策；保留分支的独有代码和证据。
不自动继续新布局、量化、全局small-M或其他优化路线，也不开展长稳。
