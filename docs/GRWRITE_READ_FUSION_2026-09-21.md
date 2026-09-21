# GRWrite→GRRead 归一化融合候选

日期：2026-09-21。父版 dd1da64，固定性能参考 fcb5925。
状态：首版相邻复核仍有 200K 不利范围，未接受；第二版向量读取已接受；完整 HTTP、逐位、资源合同与时间线通过。

## 边界与数值顺序

只融合每个 decoder 层的 attention GRWrite 与紧随其后的 MLP
GroupedRmsNorm。combined 仍是后续 MLP Write 的 residual，必须写入
全局存储，不能把“融合”解释为删除该张量。

固定形状 hs=2560/hc=4，每个 token 一个 128-thread CTA，每个 branch
一个 warp。先按原 Combine 的 residual + block*inject 计算，再舍入到
BF16；BF16 结果同时写回 combined 和源码声明的 20 KiB shared staging。
按原每 lane 八元素表达式累积平方和，保留 xor-shuffle 顺序，使用下一次
Read 的 hc_norm/eps 归一化。hs 仍以运行时参数参与除法，避免把旧除法
无意改成常数倒数乘法。编译后资源占用和逐位一致性尚待门禁后检查。

WriteAndRead 消费当前 frame 并准备一个独立空的 next frame，复用原
mix/inject 实现；预归一化入口为私有 ReadImpl 参数。缺少 combine gate、
空 token 或非目标形状走原 Write→Read 路径。gate 可按 stream 顺序复用。
错误后当前 frame 是否已消费遵循已提交 Write 的边界，不提供事务回滚。

normed 的存活掩码从 Read 两阶段扩展为 attention Read/attention Write/
MLP Read；其他视图容量、偏移不变。子层辅助 stream 汇合保持原代码。

## 预期与验收

理论每个 decoder forward 少 48 次 kernel 启动，异步分配数不变。
每个边界省去原 RMSNorm 对 combined 的两次逻辑全局读取，但新增 shared
读写及不同 CTA 组织；不能将逻辑字节减少直接换算为 LPDDR 流量或提速。

1. 构建后首项质量 11/11，输出与父版一致、服务退出 0。
2. 同一二进制五档 HTTP/输出 15/15，8K/200K decode 出现不利范围分离。
3. 仅在完整门禁通过后运行逐位边界对照：combined、next normed、mixed、
   final，以及不同 next weights/eps、自有/借用 gate、no-combine、帧状态。
4. 资源检查应证明布局保持、normed 新存活期不引入重叠冲突。
5. 时间线核对实际 kernel/分配数；有回退先 HTTP 复核，不做 profile 辩护。

工具草稿已准备，尚未执行，当前质量通过不能替代逐位数学验证。
证据 `.q4t-work/e2e/grwrite-read-fusion-20260921/`。
候选 SHA-256：`872ac95eac5a1fd1099f02a798e3fa46710b29e87b6b41648d253603b297fb8a`。
[机读合同](../dataflow-engine/plans/grwrite_read_fusion.json)。

## 初始异常与处理

相对直接父版，8K decode 18.128563→18.044600 tok/s（-0.46%），
200K 17.090991→17.044779（-0.27%），重复范围不重叠；200K 相对
固定参考也出现不利分离。TTFT 各档下降约 1.4%–1.9%，不能抵消这些
风险。完整原始结果和 comparison.json 保留，不修改阈值或选最优档位。

完整性能门禁停止了数值、资源和 profile 任务，三者均未执行。
冻结的父版→候选→父版在 8192/204800 两档各重复三次，保持原输入、
输出和启动参数；相邻复核完成前不接受候选，也不开始底层细分析。

## 相邻复核结论与第二版

三组 HTTP 均完成且输出一致。8K 范围与两侧父版重叠；200K 候选
17.041260–17.080538 tok/s，前侧父版 17.081634–17.104710，后侧父版
17.054225–17.066074。后侧重叠不能抵消前侧不利分离，首版未接受，
也不能据此断言稳定回退幅度。数值、资源、profile 均未执行。

第二版仅将融合 kernel 的 residual/block 输入改为对齐 float4 读取，
保留八元素运算、BF16 舍入及归约顺序；不对齐地址走原分离路径。
源码可见的读取方式变化不是已测瓶颈结论，是否有效重新由完整 HTTP
判定。父版仍 dd1da64，固定参考仍 fcb5925。独立证据目录
`.q4t-work/e2e/grwrite-read-vector-20260921/`，首版数据不覆盖。

第二版构建零警告，首项质量 HTTP 已启动。二进制 SHA-256：
`a7d793e58cd303aaaadb60315294ab5496d5739b39837b4cdfeab9f8e724f107`。

第二版首项质量 HTTP 11/11 通过，输出检查通过、服务退出 0；同一
二进制完整五档性能运行中，仍未执行数值、资源或 profile。

门禁后数值工具草稿增加非 16 字节对齐 block 输入的分离路径对照，
计划覆盖 5 个 token 数 × 3 个输入族 × 5 种模式，共 75 组。仅准备
工具，尚未构建执行；运行时二进制和源码不变。

## 第二版完整 HTTP 门禁

质量 11/11、性能 15/15，输出一致、服务退出 0。同一冻结二进制相对
直接父版 dd1da64 和固定参考 fcb5925 均无不利分离范围。

| 输入 token | TTFT 秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.818754 | 18.600675 |
| 4096 | 2.729325 | 17.856818 |
| 8192 | 5.380085 | 18.118351 |
| 45056 | 31.199250 | 17.864540 |
| 204800 | 162.122880 | 17.088506 |

TTFT 为三次均值、包含 HTTP/分词/prefill；decode 为首 token 后总
生成数/总时间。首版失败不覆盖，第二版独立通过完整门禁。现才开始
75 组逐位、资源合同及 HTTP 时间线，正式接受仍取决于这些检查。

## 第二版门禁后检查

- 数值：75 组，5,865,523,200 个有限 BF16 值逐位一致。覆盖不同
  next weights/eps、自有/借用 gate、no-combine、未对齐 block 输入、
  frame 消费/丢弃、normed 覆写后下一次 Write；空 token 检查通过。
- 资源合同：252 布局、504 链接容量、1260 错误合同拒绝通过，旧偏移
  和容量保持，normed 的 attention Write 存活阶段检查通过。
- cuobjdump 编译资源：融合 kernel REG=38、STACK=0、SHARED=21504、
  LOCAL=0。源码 staging 20480 字节不等于编译总 shared 21504 字节；
  不把 LOCAL=0 泛化为整个推理无 spill。
- 4K HTTP 时间线运行中，kernel/分配次数与阶段耗时尚待核对。

## 第二版最终接受与限制

4K HTTP 时间线输出通过，调用数核对：prefill kernel 87703→87655，
255 次 decode 共 442935→430695。每 forward 原 CombineWithGate
减少 48 次、GroupedRmsNorm 减少 48 次、新融合 kernel 增加 48 次；
其他 kernel 名称/调用次数保持。异步分配/释放各保持 266/forward。

带 profile 阶段窗口：prefill 3029.165→2941.546 ms，decode
14406.733→14511.677 ms。后者略长，不用局部 kernel 数下降宣称
整个 decode 提速；正式五档无不利范围分离，decode 按持平理解。
正式 TTFT 相对父版约下降 1.5%–2.0%，其中 200K 164.876→162.123 秒。

第二版接受，固定参考仍 fcb5925，不覆盖首版失败证据。combined
全局 residual、workspace 容量及 gate 所有权保持。本次证明普通
单流固定模型形状下的融合，不证明 MTP/多流，也没有测整机峰值内存
或实际 LPDDR 流量。下一步根据已验收时间线继续核对 GRRead 数据流
与生命周期，独立候选仍先完整 E2E。
