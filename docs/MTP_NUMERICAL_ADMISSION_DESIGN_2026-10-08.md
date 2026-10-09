# MTP 正式支持的数值合同选择（2026-10-08）

此页是只读代码审查后的设计，不是实现或验收结果。总目标见
[转正计划](MTP_RELEASE_PLAN_2026-10-08.md)。当前基础修复 A 独立
交付为PR #2；两种B数值合同已向用户说明，尚未收到选择。
agent按推荐的严格普通greedy方案作为可回退实现默认方向，
不记为用户确认。预计更慢，快速T4继续保留为实验路径。

## 当前快速路径的事实

已证明的具体 RoPE/checkpoint 缺陷已修复。局部投影包络、同形
k3 回放和新旧优化 exact 均有有效证据，但它们不能解释所有
plain T1 / verify T4 的整模型生成分叉。

当前两种形状会切换 HC GEMV/GEMM、GDN reduction 次序、短上下文
indexer、QSA decode/prefill 和 MoE M1/真实 M_e 分组。算术次序
不同本身不是缺陷；独立 reader 与同一 T4 writer 回放也不能
单独证明 writer 的逐 token 算术。禁止由实测误差倒推容差。

自然 bonus=287 的旧首个差异位于第 0 层 attention 输入 x，
4/2560 元素不同；已完成的独立投影包络针对固定 token=97 的
qkv_raw。若继续快速路径归因，下一固定对象应为自然例的 HC
normed/mix_down/SiLU/mix_up/mixed；复用旧 prompt 和 suffix，
限定 M1、原 M4、仅改 suffix 的 M4 三路，先验算术包络与 suffix
独立性分别判定。不能再以已解释的 fixed97 投影代替自然首分歧。

若允许快速 T4 作为独立有限精度执行模式，仍需补实际 recurrence
writer、有效 KV/indexer/位置/history、未来缓存不可见性和离散
决策的独立合同，再进行预先固定的任务质量筛查。该路径不称为
与普通 greedy 无损等价，不用总体 L2 小掩盖 router/QSA 选择变化。

## 严格普通 greedy 一致的可实现基线

可新增独立 sequential target step，保留真实 draft 生成、接受/
拒绝和 draft extend，但每个 target token 走普通 scheduler 使用
的 `ModelDecodeBatchMulti(B=1)` 与 `ArgmaxBf16Rows`。

令其有限精度状态转移为 F(state, token, position, history)。
从相同 prefill 状态和 bonus 开始，每次 target 调用参数相同：
接受意味着 draft 已等于普通下一 token，拒绝返回普通 correction。
按此逐步归纳可以保证实际输出 token 与普通路径一致，无须发明
跨形状误差上界。直接测试用于验证接线与所有权满足证明前提。

具体状态流：

1. 使用真实 d0/d1/d2；main 只输入 bonus 和已经被接受的前缀。
2. 局部 cursor 构造 oldest-first PLE history；不修改 caller 的
   position/history，完成后仍由请求线程发布接受前缀。
3. 首次不匹配即停止，main 从未处理拒绝 token，无须 checkpoint
   回滚；使用实际 target trunks 完成 EAGLE shift 与 draft extend。
4. target 遇任意配置的 stop token，不将 stop 再输入主模型。
   terminal 可跳过 extend，但必须明确 next_seed 不再有效。
5. 输出或上下文剩余不足完整 step 时，使用同一 scheduler B1
   普通尾部。不能完整计算后才截断输出并声称状态等价。
6. main forward/extend 中途失败终止请求并排空，不从部分已改变
   的 main 状态静默改走普通路径。

代码追踪确认 S1 普通 prefill 实际走 `ModelPrefill`，与 MTP
inline 相同；长输入两者使用 `ModelPrefillTextChunk`。要核对额外
trunk capture 不改变 logits/状态，无须先改 prefill 算法。
`ModelDecodeStepSeq` 的失败后备入口不等于 scheduler B1；正式
strict 合同不能把它默认为同一参考。

该基线每个输出至少一次普通主模型 forward，外加 draft/init/
extend，因此预计慢于普通 decode。它提供正确性参考，不能称
已有加速成果，不能用历史 fast T4 性能数字宣传它。

## 两种模式均需明确的证据边界

- `ModelSnapshotState` 只有 recurrent。主 KV、idx_raw、已完成且
  可见 idx_comp、page table、RoPE 和 host history 须按有效读域
  单列，废弃未来字节不要求与普通轨迹相同。
- 未来缓存污染检查应固定两套有限值、同前态/同输入/同形状，
  检查下一 target logits/trunk/有效状态精确保持；不能用改变
  suffix 的试验替代，因为 suffix 会改变真实 MoE 分组。
- 受控接受 0/1/2/3 分支与自然 draft 轨迹分开，不为凑覆盖扫描
  prompt 或 seed。完整 argmax/tie-break、accepted prefix、
  correction 和 next seed 独立核对。
- HTTP 日志必须区分实际 verifier，strict 不得继续记作 T4
  `mtp_multi_b1`；旧 MoE v1/cycle schema 若不匹配应明确拒绝，
  不填造假的四行计数。实际 fallback 不计为 MTP 通过。
- 短六位答案 fixture 不代表持续生成质量。最终请求数、输入、
  独立答案/评分合同及数值/质量/性能出口须在新模型测试前冻结。

目前未实施 sequential target、未为本设计运行模型或 HTTP。
只有入口 B1 修改已形成独立待验收快照，不能据此将 B 标为通过。
