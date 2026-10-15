# 数值诊断阶段2（2026-10-16）

## 独立参考（修复前诊断，原阈值/FAIL仍保留）

- checkpoint独立前缀执行与恢复输出逐位一致，a=0/1换错row负控均正确检出。
- B=3 ragged prefill与三个独立single-sequence前缀在相同总M=15时，
  所有token logits逐位相同；原不同M=5/3/7对照仍有L2/argmax差异。
  最早的已采样差异在layer0混合投影、早于recurrent状态更新。
- 旧MtpSpeculativeStep的reject/partial/full后继输出，均与不运行draft/
  checkpoint restore、仅执行实际接受前缀的独立参考逐位一致。
- 这些证据支持修改跨形状“应近乎逐位相同”的测试前提，而不是抬高L2阈值。
  本阶段未删除原断言，测试仍FAIL；最终合同还需统一落地并复验。

## 实际运行时缺陷：PLE pinned staging复用

PleEmbedding::GatherRows在前次异步H2D未完成时，可再次由CPU reader覆写
同一pinned staging；cudaStream顺序不能约束该CPU写入。控制首stream队列
暂不执行，连续读取FP8 1.0/2.0，修复前首次输出160/160错误、第二次正确。
这是独立于舍入的真实缺陷，不能以有限模型测试通过掩盖。

修复为对象持有上传/转换完成事件，在下一次CPU reader写入前等待；事件
覆盖conversion以保护不同stream的共享FP8 scratch。析构等待owned工作；
提交错误出口排空，完成失败禁止复用。并发host调用仍不支持。

修复后4项PLE合同通过：CPU参考、容量拒绝、同stream重复、跨stream重复；
重复测试同时覆盖pending状态下析构。two-layer samepath独立参考仍通过，
原跨形状L2失败仍保留。完整256步通过（flips=4、confident_both_sides=0、causality通过）。
取消调用方人工等待后，single/samepath的partial仍为0.02632、full为0，
独立前缀状态/输出和错row负控通过；较大的0.12970/0.26336不再出现。
这将此前等待敏感现象与上传所有权缺陷关联起来，而不将其归为舍入。
最终HTTP质量/五档性能尚待收口，不混用旧binary验收为新版本背书。

完整capture留本机原路径并附摘要，未复制模型权重。
