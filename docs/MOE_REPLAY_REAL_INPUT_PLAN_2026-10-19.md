# 主干 MoE 真实文本输入与分项差异报告

状态：ACTIVE；用户2026-10-19授权继续并设置目标。起点fa9a6cc。
这是A1后的新诊断阶段，不追认合成夹具为真实文本隐藏状态。

## 完成条件

1. 从固定真实文本、实际tokenizer和显式teacher-forced token序列得到
   主干MoE层输入，保存完整前缀/逻辑位置/层/形状及来源身份。
2. 真实层输入正常记录→同形状回放，IDs、weights与MoE输出逐位一致。
3. T1/T4受控对照包含正常路由；报告分开列出输入hidden、专家选择、
   选中logits/weights及输出差异，可从保存的原始字节独立复算。
4. 严格身份、无缺行、文件完整性和完成边界通过；生产路径保持，
   验证证据、复现命令、状态/日志与可回退提交推送完成。

不扩展草稿MTP、完整MTP trace、offload、预取、驻留偏置或Graph。
不新增冻结路由权重机制；不能把固定IDs后的差异全部归因于专家计算。

## 已核对的入口及选择

- `MoEForward` 是独立对象中的外部函数；`DecoderLayerForward` 传入
  实际 `layer_id`、`d_mixed`、原权重和stream。可用独立可执行目标
  的链接包装捕获，不改生产源文件或全局observer职责。
- `tools/verify/moe_first_decode_observer.cpp.in` 依赖4096行/48层计数
  推断层和位置，不具备本目标要求的完整token身份，不能直接复用。
- `tests/mtp_verify_numerics_test.cpp` 已使用本模型tokenizer、真实模型
  加载、`ModelPrefillTextChunk`、`ModelDecodeStepSeq`和`ModelDecodeBatch`。
  复用这些公共接口与正确的前缀/state生命周期，不复用未知token的旧trace。
- 目前模型配置默认vocab=248320；新夹具必须读取/核对真实配置和tokenizer，
  不沿用A1合成夹具入口中150016的任意token检查上界。

链接包装方案需在构建阶段核对函数符号与实际命中，不能把“链接成功”
当捕获正确。包装入口收到独立驱动显式设置的调用上下文：完整token
前缀、起始位置、rows、阶段/调用ID；缺上下文、错层/形状或重复缺行
均失败。只选定主干层2；所有原计算通过真实MoEForward继续执行。
捕获侧等待自身stream完成后发布，结果与输入所有权不得依赖临时指针。

## 有界实现范围

- 一份版本化真实文本夹具，tokenize整份文本后明确分割prompt与4个
  teacher-forced续接token；保存UTF-8、tokenizer摘要和完整token IDs。
  不依赖自由生成恰好产生某段文本，不用位置猜测身份。
- 一次全模型加载；普通单流、无草稿MTP、全驻留；独立重置和相同prefill
  后比较四次T1与一次T4。prefill目标不超过256 token，续接4 token。
  具体容量/PLE路径须按实际加载合同冻结，不先假设可以缩小所有池。
- 分开保存两类比较：真实T1/T4前向到达层2的输入可能已经不同；相同
  layer-boundary输入的T1/T4重放才能隔离本层形状因素。报告不得混淆。
- 选择干预若把T4 IDs用于T1，必须显式记录来源route SHA、逐行token
  前缀/位置映射和目标hidden摘要。不得静默修改A1记录的source身份，
  不放宽原同形状Prepare对hidden和shape的严格匹配。
- 离线报告至少给出有序slot差异、专家集合差异、按expert对齐的当前
  logits/weights差异、bitwise计数/max_abs/L2、有限性、输入hash及来源。
  不对专家未对齐的slot权重直接给出误导性比较；跨形状只报告事实。

## 验证安排与停止条件

先完成捕获合同、夹具读取及离线报告的host负控与构建，再统一GPU。
第一次GPU前发布可执行plan.json，冻结精确binary/模型/权重/token身份、
命令、前向次数、重放次数、lease与输出路径。当前GPU预算为0（准备期），
这不是最终阶段上限；实现清单明确后冻结一次完整验证，避免中途试跑。

host阶段检查：错token/位置/层/shape、缺行/重复捕获、截断与摘要错误；
报告对人为构造的不同集合、重排、同分、NaN和已知数值差异给出正确
统计并拒绝无效归因。正常路径通过生产对象与链接依赖核对；若完全
不变，复用A1/B1b已有HTTP证据，新增HTTP为0。若实际影响生产执行，
测试前另冻对应HTTP保护，不以独立工具名义省略。

任何失败保留现场；仅对确认缺陷做受影响重验。GPU完整驱动统一持锁，
ENV_ABORT不算数值失败。局部合同不代替整模型任务质量或N=4合同。

## 交付

独立真实文本capture入口、版本化layer-input文件、A1重放适配、离线
分项JSON/可读报告、固定夹具、复现命令、身份清单与原始证据。
沿用当前main，以提交划分阶段；默认build/q4t及模型/reference只读。

## 实施进度：host合同（2026-10-19）

已实现 `tools/verify/moe_replay_real/` 的文件读取/离线报告和显式
CaptureContext。8项报告测试＋1项上下文测试通过；严格字段/大小/摘要、
完整前缀、专家对齐及错误后禁止发布均有负控。详见
[host结果](evidence/moe-replay-real-20261019/host-result.json)。
这不是实际模型捕获或GPU结果；链接包装、真实文本驱动及A1重放接入
仍待实现。当前生产源文件未改，GPU/HTTP新增0。
