# 独立静态复核（未执行）

审阅者仅阅读源码、历史原始结果与冻结文档。没有构建、运行测试、启动
HTTP/GPU，也没有修改 tracked 文件。可执行命令正文保存在
`http-driver.py.txt`；保存它不代表已运行。

## 冻结门禁与 EVALUATION 的一致性

- `docs/EVALUATION.md:116–120` 要求同输入、每档至少3次并保留首请求；
  3次不能支持稳定尾延迟或微小提速声明。
- `docs/EVALUATION.md:150–152` 要求精度保持、每档无超出已有重复波动的
  回退，短档不能抵消长档。当前门禁保留逐档 TTFT 和 decode 的原始3值；
  不增加任意容忍百分比，不将范围相交称为等效。
- 使用原范围整体不利分离作为本轮 NO_GO 判据契合旧 bounded 实践，
  但只是预先固定的判别规则。跨时段历史基线仍有系统状态差异；单侧
  3个样本的 min/max 不是置信区间，范围分离也不能单独确证因果。
- 因本候选增加 restore/snapshot 表示逻辑，性能持平不满足“复杂度下降
  且性能持平也接受”的例外。至少一档 default-on decode 有利范围分离，
  且五档调和平均吞吐比的几何平均 > 1，作为有限的可辨识收益门槛。
  不继承旧任务 +5%，不扩大样本寻找通过结果，不声称五档全部提速。
- 第3条文字应使用“吞吐比几何平均 > 1（增益 > 0）”；单写“吞吐比
  几何平均为正”没有判别力。根 agent 已同意更正文档。

## 有限执行矩阵

| 顺序 | 内容 | 正式请求数 | 继续条件 |
|---|---|---:|---|
| 1 | 默认 MTP on 固定质量 | 11 | 精确文本/期望、长度、finish、退出全部通过 |
| 2 | 默认 MTP on 五档，每档3次 | 15 | 相同输入/输出、确定性、256输出、完整矩阵；无逐档回退且有冻结收益 |
| 3 | MTP off 固定质量 | 11 | 同上质量合同 |
| 4 | MTP off 五档，每档3次 | 15 | 完整矩阵；无逐档 TTFT/decode 回退 |

全部通过共52请求；无额外warmup或隐藏探测请求。on 不具收益或出现
回退则保存证据并停止，不再执行 off 和无必要的细分析；未执行项标记
未运行。无论输出HTTP阶段是否通过，都不能提前宣布数值/整体验收通过。

逐档 throughput 用3次 decode_tps 的调和均值；固定每次256输出时等于
总765个 decode token / 三次 decode 时长之和。对5档 throughput ratio
取几何平均，再减1为增益；不混用旧摘要的算术平均。

精确判断：回退为 `max(new_tps)<min(old_tps)` 或
`min(new_ttft)>max(old_ttft)`；有利吞吐为
`min(new_tps)>max(old_tps)`。边界相等属于未分离。仅 on 需要收益；off
属于公共路径回归，不要求获益。任何档的回退不能被均值掩盖。

## 身份与命令注意事项

- baseline 是 main 28a3783 运行时，binary SHA256
  `be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072`。
  四组 raw fixture/reference 全部来自主 workspace 绝对路径
  `.q4t-work/mtp-numerical-closure-20261016/http/`，不是 HC 另 chat 候选。
- candidate 固定为
  `/home/rm01/models/dev/qwen4-thor/.q4t-work/gdn-record-fold-20261016/build/q4t`，
  保留实际相邻 CMakeCache.txt。source 在同级 source/。
- runner 使用主 workspace 的已安装 evalscope。它的 commit.txt 和
  worktree.patch 描述主 workspace；不能据此宣称 candidate 来源。
  driver 在每组采集前后另存 candidate source commit、source.patch、
  status、文件SHA清单、实际source路径、binary/cache SHA，要求全程一致。
  build 日志仍由 root 单独保留以证明零警告与 source/build 对应关系。
- 不传 `--perf-lengths`，即使列表列齐5档也不能传：该选项切成每请求
  单独 client process，与历史完整矩阵的 client 生命周期不同。
- on 用 `--mtp-default` 测真实默认路径；off 没有 MTP 参数，runner 会
  传 `--no-mtp`。固定 max-seq1/max-prefill8192/max-len208896/default k3。
- runner 自己只清理部分 Q4T 环境变量；driver 将所有继承 Q4T_* 移除，
  记录名称，防止诊断时间/层时间或精度开关漏入正式矩阵。保留并记录
  CUDA_/NVIDIA_ 环境；CUDA_LAUNCH_BLOCKING 不为0则停止，不能悄悄计入。
- 输出固定新目录 `.../gdn-record-fold-20261016/http`，已存在就拒绝，
  不覆盖旧证据。具体实现缺陷需要修复时由 root 显式开新输出目录；
  该准备脚本无自动重试、扩样本、吞掉失败或自动启用候选的逻辑。
- runner 的 performance_acceptance=false 是设计上的“本工具不裁决
  性能”，不是 HTTP 失败。driver 另存 per-tier comparison；通过标记
  明确为 HTTP_SCREEN_PASS_NUMERICAL_ACCEPTANCE_PENDING。

## 冻结实现仍需注意的风险

1. “每slot row0完整、row1起紧凑”必须仍按 physical cap/pooled slot
   stride 计算槽基址；不要使用当次 num_ckpt 缩小跨层/跨seq步长。
   `num_ckpt>=2` 与所有层 `kd*vd>=kd+vd+2` 是容量必要条件；同时
   record/fallback 的 latest 表示必须一致，并在失败/reset/普通forward
   之后失效。不能依据当前 A/B enable flag 解释上次已生成的数据。
2. 记录 FP32 normalized K/delta/alpha/beta，重放同样带括号的
   `alpha*old + (beta*k)*delta`，仍不自动证明编译后逐位相等。维持旧
   verify 的 kS 归约及 FMA 行为，禁止把 beta 移到 delta 或折叠次序。
   B1 生产入口经过 multi kernel；只通过 single kernel 差分不够。
3. a0复制anchor、a1/a2从不可变anchor重放、a3保留livefinal；重复restore
   不能从已恢复live再重放。拒绝尾的 NaN/poison 必须不参与读取/算术。
4. diagnostic Snapshot scratch 要有失败清理且不污染live/anchor/record。
   raw pointer 诊断显式选旧表示，不能把record当完整行。新数值合同必须
   断言record dispatch，避免全部fallback取得假绿灯。
5. 原分配/预算保留，静态减少的是写payload212.5986328125MiB/verify；
   不能宣称分配节省、带宽实测或吞吐获益。partial restore 额外kernel/
   读写可能抵消少写收益；只有固定HTTP矩阵能裁决本候选实际价值。
6. 模型 API 的 multiseq 需要回归 pooled slot/非连续 seq_id/重复seq拒绝；
   HTTP server MTP 仍是 max-seq1，不扩展对外多并发支持声明。

冻结文档所列后续同形状数值差分、近tie/causality、生命周期和恢复边界
检查有必要；仅在 HTTP 阶段通过后统一执行。本文不扩写测试矩阵，避免
与负责实现/测试的 agent 重复工作。HTTP门禁成功只表示可进入这些检查。
