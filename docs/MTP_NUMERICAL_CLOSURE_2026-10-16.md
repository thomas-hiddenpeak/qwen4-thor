# MTP 数值收口（2026-10-16）

当前完成局部五项合同，修复后HTTP质量/五档矩阵正在运行，PR #4保持草稿，
main未改动。运行时SHA256：
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

## 整模型验收（待完成）

新运行时的off/on五档各3次、两侧HTTP质量、尾部/取消恢复正在收集。
旧`26639190711e…`的性能和质量记录只作历史对照，不作为本版本通过证据。
阶段提交仅冻结可回退边界，不代表main合入批准。
