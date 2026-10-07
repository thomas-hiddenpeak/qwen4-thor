# MTP 分歧定位与收益判定（2026-10-07）

本阶段由用户明确授权继续 Goal，沿用
`codex/mtp-admission-20261007`，起点 `c365b9b`。上一阶段的
[NO_GO 与原始失败](MTP_ADMISSION_2026-10-07.md)保持原样；本轮尚未
取得正确性或性能接受结论。默认 MTP 关闭，不自动合并或部署。

## 冻结范围与执行顺序

单 Thor、文本 greedy、B=1、k=3；正式服务 max_len=208896、
max_prefill=8192，模型与精度不变。只调查已观察到的普通 decode 与
MTP verify 分歧，不新增性能候选、offload、媒体、多流或动态 k。
模型目录和 reference 只读，原两个 dirty 工作区、main 与默认
二进制独立保护。产物进 `.q4t-work/mtp-continuation-20261007/`。

1. 先完成三个互补的直接诊断，再根据具体失败决定是否补修。
   不修改生产算子来强行取得逐位相同，不改旧 admission 的失败记录。
2. 发现实现缺陷时保存最小反例，成组修复、重验受影响合同及 HTTP
   质量/生成边界；未受影响且依赖身份相同的证据明确复用。
3. 只有实际数值、接受/拒绝/回滚合同闭合，才进入五档 off/on
   HTTP 收益验收。局部算子通过或固定短答案通过不能替代整路径。

## 第一组直接诊断

本组只新增测试，链接当前生产实现；不启动全模型 HTTP、不运行 bench。
同形状、同路由、同前缀的独立行必须不受其他行输入影响；跨形状的
逐位差异单列诊断，不把它自动判为计算错误，也不倒推新的容差。

- **入口三角对照**：实际四层、原 13-token prompt（42+17i）、默认
  GDN，不加载 draft。首 token 分别为固定 97 与 prefill 自然 argmax。
  每例重新建立同一 prefill 状态，比较 plain T=1、verify T=1、
  verify T=4 的首行，以及 T=4 改后续三 token 的结果。suffix 分别
  为 131/211/307 与 503/701/907。记录全 vocab、top logits/margin、
  trunk、恢复 checkpoint 0 后的 recurrent state。plain/verify T=1
  使用相同算术分派，须逐位一致；T=4 差异仅定位。改变 suffix 可能
  改变 MoE 的每专家行数，因此整模型 suffix 差异也不直接判泄漏。
- **首个投影对照**：layer 0 的真实 BF16 in_proj_qkv，使用旧捕获
  `lin_m3.x.bin` 固定实际输入；比较 M=1 GEMV、M=4 GEMM，以及只改
  M=4 后三行。每个输出用 FP64 dot 与由 K、FP32 unit roundoff
  推导的累加误差区间、最终 BF16 RNE 区间独立核对。区间在运行前
  定义，不能用实测误差拟合；该证明仅限当前有限输入和此投影。
- **MoE 分派对照**：layer 0 实际权重、一个固定合成 BF16 输入、
  固定十专家及权重。A 为 M=1 grouped；B 为 M=4 且后三行使用不相交
  专家（首行每专家 M_e=1）；C 为四行相同专家（M_e=4）；D 保持 C 的
  形状/路由，仅改后三行输入。A/B/C 差异量化报告；C/D 首行必须
  逐位一致且有限。可读取已分配 workspace 中各专家 down 输出作
  独立 combine 检查，不追加权重载荷审计或无界 capture。

三项各首次运行一次，串行占用 GPU。构建和工具故障保留；修复具体
故障后才定向重试。最多对首个未解释边界增加一组操作数/状态诊断，
不通过不断换 prompt、算法开关或容差寻找通过结果。若本阶段边界内
仍不能解释真实生成分叉，则以定位所得和下一步缺口 NO_GO 收尾。

## 数值、生成与性能出口

同路径状态恢复、接受到首次拒绝、校正 token、长度/停止/取消按精确
合同判定。有限精度的不同执行形状不天然要求 bitwise 相同，但必须
说明实际执行的格式、累加、舍入与因果状态语义；不能把四层随机 token
上的语义差异或所有跨模式不同笼统归为 near-tie。

已存在的两层 0.01 相对 L2、局部 GDN CPU 参考 0.02 均不转借为
四层/整模型容差。必要时对已定位首分歧做独立算术验证；旧错误路径
只作兼容对照，短答案 HTTP 质量独立报告。

进入收益验收后，使用现有 tools/evalscope 固定同输入五档
1024/4096/8192/45056/204800、每档 3 次、输出 256，off/on 分组。
保留全部首次请求、后续请求、输出/usage、实际 MTP 路径、容量、
客户端 TTFT/decode/整请求时间和资源观察。没有精确 token 时间戳
则不推导 ITL 分位数；不同输出和算术身份必须列明。

五档均不得超出基线重复波动而回退，不预设任意容忍百分比；所有档
完成后才作收益接受判断。若正确性未闭合则不跑性能接受矩阵；若
正确性闭合但收益不足，保留修复并明确性能 NO_GO，不自动继续优化。

## 证据复用与身份

起点二进制 `7fe34d6ddd6bc78c4c9dea6537f72802c93fc60f09eb0b977105660480c627c7`。
上一阶段资源/生命周期、RoPE、checkpoint、HTTP 质量和边界证据
仍绑定其原受测身份。新增测试本身不改变服务实现；后续若改运行时，
按实际依赖重新界定复用范围。

阶段目录 `starting-identities.json` 保存原工作区 diff、main、默认
与起点二进制、模型库身份；不哈希模型载荷。过程与结论在本文件
补充，历史日志仅追加。

## 首次诊断结果与唯一后续状态组

测试二进制 `8a372182` 零警告；服务 `7fe34d6` 与模型库 `473aa421`
均未改变。三项各首次执行一次，全部通过、无 skip。现有 hook 同步
采集 451 文件、47,292,416 B，完整绑定 manifest；不用于计时。

- 投影同实际输入重现 19/10240 个跨形状差异，最大绝对差 0.03125。
  M1/M4/改 suffix 的每个输出均在预定义 FP32 累加加 BF16 RNE 包络
  内，M4 首行不受后三行改变影响。这解释该投影有限样本，不给整模型
  误差上界，也不把理论包络当作新的全模型相对 L2 容差。
- MoE A/B/C/D 的首行全部逐位相同，A/B 的 25,600 个专家 down 元素
  及独立 slot combine 均相同；未发现该固定输入的分派差错。
- 八次 prefill 的完整持久状态及输出相同；两个首 token 的 plain
  T1 与 verify T1 输出/trunk/recurrent state 均相同。两组 T4 更换
  suffix 后首行及 checkpoint 0 状态相同，跨 T1/T4 仍有差异。
- 首个自然分歧有明确数值：plain 的 token 220 和 359 同为 5.1875，
  最低 ID 规则选 220；verify T4 为 359=5.125、220=4.96875。普通侧
  确为同分，但 verify 有不等量位移，不能只称 tie-break 差异。
  第 0 层 MoE 前后已放大输入差异；没有 router 采样，不宣称专家翻转。

既有 k1 强制接受/拒绝和 cap1/cap3 证据不覆盖实际 k3 的全部选择
结果。因此冻结唯一后续状态组：完整 48 层、正式 208896/8192/S1、
既有 HTTP 1024-token 原始 prompt，16 次自然 k3 加一次预定强制首
draft 拒绝。每步读取实际 drafts、全部 target logits 与 trunk，
由独立 host argmax/前缀检查验证返回的 count、accepted、correction，
同时核对 next_d0 与 next_g 的实际 extend 来源和 caller 状态所有权。

每步保存 main recurrent 前态，复放完全相同 T4 输入，比对全部
logits/trunk，再按接受边界核对后态；首步另独立读取三个 checkpoint
原始行并检查逐行恢复。此为 S1 的有限诊断，snapshot 仅含 recurrent
state，不声称保存了全 KV；同位置重算覆盖 speculative KV/indexer，
其未来位置由因果选择屏蔽。不得为凑齐分支改变 prompt 或追加步数。
记录实际观察到的接受数，缺少的自然分支不冒称已覆盖。

强制拒绝的 seed 由该位置一次固定 suffix T4 target argmax+1 产生，
恢复前态后执行实际 step；若改变 suffix 影响 target 导致未拒绝，
明确记未覆盖，不自适应试种子。小型原始 logits/trunk 保留，recurrent
状态只记比较结果，不保存 GB 级完整状态或权重载荷。

不同形状计算不是现有规范中的逐位合同。只有上述具体算术/状态
合同通过且没有未解决的执行反例，才进入固定配置的**实验性收益
测量**；跨模式生成差异仍单列，旧 admission 的逐位诊断失败保留。
这不等于证明整模型误差有统一上界，也不自动批准 MTP 启用。

后续测量复用相同服务身份的 `performance-off-04` 五档 15 条作为
历史基线，仅新增 MTP-on 同输入五档 15 条；不会看到结果后更换
基线。普通/MTP HTTP 质量、长度与资源故障证据按未变身份复用。
资源分别列 RSS/HWM 与系统 MemAvailable，不将历史/本轮之差称为
当轮受控的总物理 RAM 增量。

## 完整 k3 状态组（2026-10-08）

首次运行 `mtp_k3_full_model_replay` 通过，无 skip，53 秒内完成。
新增测试零警告，测试二进制 `aea26ebc`；服务 `7fe34d6` 与模型库
`473aa421` 不变。实际 48 层、208896/8192/S1、原始 1024-token
prompt，恰好 16 次自然 step、一次预定强制 step、17 次同形回放和
一次额外 seed probe。未追加请求或改变 seed 寻找通过结果。

自然 count=1/2/3/4 分别出现 2/1/2/11 次，即实际接受 0/1/2/3
draft 全部覆盖；强制步骤实际 count=1。每步独立 host argmax、
accepted prefix/correction、caller 状态、next_d0/next_g 均通过。
全部 17 次 target logits、trunk、接受后 115,642,368 B recurrent
状态逐位相同且有限；首步三个 raw checkpoint 行与独立读出相同。
原始小型证据 46,300,139 B，结果及逐文件 SHA 见阶段目录
`k3-replay-01-result.json`，完整输出见 `k3-replay-01.log`。

raw checkpoint 比较独立于生产 reader，验证读取布局；它不独立证明
writer 的逐 token 数学结果。同形回放限同位置、相同四 token、相同
history 的 S1/slot0；不等于保存全部 KV，也不证明跨形状等价。
上述具体算术与执行合同未发现未解决反例，允许进入固定五档实验
收益测量；旧 cross-mode equality 失败仍保留，默认启用未获接受。
