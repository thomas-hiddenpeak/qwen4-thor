# MTP 数值收口（2026-10-16）

五项局部数值诊断已收口，PLE上传缺陷已修复，新运行时五档性能、两侧HTTP
质量及尾部/取消恢复全部通过，可进入合入审查；main未改动。运行时SHA256：
`be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072`。

## 因果证据与修复

[阶段1](evidence/mtp-numerical-closure-20261016/phase01/README.md)定位首个
已采样差异为layer0 beta的1 BF16 ULP；独立高精度点积证实跨形状参考并非
始终更精确。不能以这种参考的L2差异直接断言checkpoint损坏。

[阶段2](evidence/mtp-numerical-closure-20261016/phase02/README.md)发现独立的
真实PLE pinned staging异步复用错误。受控复现首输出160/160错误；对象
持有上传完成事件后，同stream、跨stream、析构及CPU参考共4项通过。
不再依赖调用方人工等待。完整256步近tie复验通过：4 flips、0双侧confident、
causality PASS。以上证据在运行时身份不变时复用。

## 五项正式合同

[阶段3记录](evidence/mtp-numerical-closure-20261016/phase03/README.md)：
`model_verify_ckpt`、`model_verify_multi_ckpt`、`model_verify_ckpt_samepath`、
`model_prefill_batch`、`mtp_spec_accept_states`共5/5 PASS，0 skipped。

前三项恢复机制采用不保存/恢复checkpoint的独立前缀参考，后继logits逐位
一致；samepath检查partial/full状态与输出、错row负控。批量prefill用相同
总M=15的独立普通prefill，3个序列各2种填充，全部token逐位一致。旧MTP
拒绝/部分/全接受保留语义检查，并与无draft/restore的独立执行逐位比较。

原不同形状prefill L2/argmax观测仍保留：例如旧MTP partial/full分别
0.04154/0.07696，跨形状0.02观察线未提高。这些数据不再用作恢复门禁，
因为独立投影与状态证据否定了该门禁的计算形状不变前提。新门禁要求
逐位一致，不能用近tie容差掩盖恢复错误。覆盖范围和限制见EVALUATION。

## 整模型验收

新运行时off/on五档各3次完成，确定性、HTTP输出合同全部通过；十组文本
均与修复前一致。两侧质量各11/11，输入长度与HTTP输出检查均通过；
六项尾部请求、流式取消和后继恢复通过，所有服务正常退出。
完整记录见[阶段4证据](evidence/mtp-numerical-closure-20261016/phase04/README.md)。

| 上下文 | off decode_tps | on decode_tps | 增益 |
|---|---:|---:|---:|
| 1024 | 18.639 | 34.523 | +85.2% |
| 4096 | 17.878 | 35.464 | +98.4% |
| 8192 | 18.141 | 37.385 | +106.1% |
| 45056 | 17.874 | 35.887 | +100.8% |
| 204800 | 17.145 | 33.168 | +93.5% |

全部满足+50%～+200%区间。相对PLE修复前同口径冻结测量，off吞吐变化
−0.36%～−0.09%，on变化−0.05%～+0.15%，不宣称每个测量值均无下降。
旧`26639190711e…`的性能和质量记录只作历史对照，不作为本版本通过证据。
最终测试构建零警告。阶段3后的测试改动仅澄清注释、诊断打印与格式，
未改变门禁逻辑；复用5/5结果，不重复完整推理。阶段2的PLE四项及256步
结果按相同运行时身份复用。公共CI的最终状态见[PR #4](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/4)。

质量数据库只保留EvalScope成功标志，HTTP状态码字段为null；不虚构200。
归档工具已按实际schema检查成功标志与来源说明，首次字段假设失败单独记录。
尾部/取消脚本直接记录真实HTTP状态。该差异不改变模型验收结果。

## 默认HTTP路径与结论范围

五项局部诊断覆盖2层受控模型、低层ModelDecodeBatch、B=2/3多序列及旧
MtpSpeculativeStep；默认完整模型单流HTTP实际使用ModelVerifyMulti与
MtpSpeculativeStepMulti。PLE对象为共用运行时，因此发现其上传竞态后，
必须重新测完整模型，不能只凭旧API或局部逐位参考交付。

当前完整模型五档的输入提示身份与修复前相同，off/on十组输出均未变化。
这证明本次固定单流矩阵的可观察行为保持；局部恢复合同不承诺任意batch
形状、任意提示的逐位不变。N=4近tie合同仍是单独验收层，不用于放宽恢复
机制的逐位检查，也不把有限质量题目通过扩大成整模型任务能力保证。

## 阶段与回退边界

- `9be8478`：独立前缀状态与投影中点诊断，保留原失败。
- `9879cda`：PLE上传所有权修复、异步反例及独立参考扩展。
- `21226b2`：五项正式局部合同收口，生产代码保持上一阶段身份。
- 阶段4证据与本报告随最终验收提交冻结；运行时仍为`9879cda`的身份。

MTP运行回退使用`--no-mtp`，保留PLE上传正确性修复。旧冻结二进制和main
历史保留用于差分及源码回退定位，不将含已知上传竞态的旧实现作为正确性
基线。工作分支继续作为后续性能基础，本目标不自动合入main。
