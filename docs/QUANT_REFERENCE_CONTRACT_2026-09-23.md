# 独立量化参考：已接受基线的格式与尺度核对

## 本轮结论与边界

已接受运行时仍为模板修复9a62371，部署二进制2392f6b1。
未改运行时，未重新运行或追认拒绝的E4M3候选。固定质量11/11、
独立答案14/15的既有边界不变，4K求和仍错；本轮不证明错题根因。

独立格式检查确认当前FloatToE4m3不完全符合其声明的最近偶数舍入
和有限饱和规则。checkpoint全量标量读取则确认gate/up合并尺度
假设成立，但down输入尺度必须逐专家保留。不能将不同尺度规则的
框架输出当作本项目的唯一数值真值。

## 为什么此时可以做数值核对

对象是已经完成HTTP门禁、随后恢复的原接受版本，不是失败候选。
检查前重新核对build/q4t的完整SHA256：
`2392f6b1fb33f28744d0e63812e47be98b4c6f224e22f087bd627d050f8bbe9e`。
format.h逐字节同9a62371，其SHA256为
`24c372233cebbb73ca88ee9b69e8937c4bf97cf2dbb096975eb85132eed24247`。

重新读取原固定质量SQLite/SSE的11条请求、全文、长度和结束原因，
核对11/11及二进制绑定；同时核对原messages完整验收记录，及刚结束
相邻对照中两组同一父版五档各15条的原始审计/正常退出记录。
恢复首项HTTP三题的原4K错误继续保留，不称为三题全对。

只使用该接受版本的header快照构建独立检查器，不链接build中的
候选库。没有通过低层测试覆盖候选E2E失败；所有拒绝决定保持原状。
证据绑定在`.q4t-work/prepared/accepted-e4m3-contract-20260923/`
的`gate-evidence.json`。

## E4M3独立参考与结果

检查器：`tools/verify/verify_e4m3_contract.cu`。期望值不调用项目的
编码器或解码器：直接按指数/尾数定义枚举127个非负有限E4M3值，
按最近距离选择，距离相等选择偶数编码。另与CUDA 13.3的
`__nv_cvt_float_to_fp8(..., __NV_SATFINITE, __NV_E4M3)`交叉比较；
本地cuda_fp8.h明确约定最近偶数舍入。

覆盖127个精确值、126个相邻中点及两侧float32邻值、FLT_MAX，
以及全部32640个有限非负BF16位模式。合计33146条、32893个不同
输入；不是穷举float32，也不覆盖负数、NaN、Inf。

| 对比 | 不一致记录数 |
|---|---:|
| 项目host编码 vs 独立参考 | 147 |
| 项目device编码 vs 独立参考 | 147 |
| CUDA原生转换 vs 独立参考 | 0 |
| 项目host vs device | 0 |

147条包含重复输入，实际130个不同失败值。次正规区27条/22值，
正常区120条/108值。构建退出0、零警告；检查器退出1，明确是
发现缺陷，不是“预期失败所以通过”。完整每输入记录在cases.tsv，
全部失败在analysis.json，退出和源码/工具二进制摘要在exit.json。

具体反例：

- 精确256应编码120，当前编码126，即448；288至416的精确值同样
  错误。当前`exp > 14`提前饱和，而有限E4M3还包括exp=15的部分值。
- 2^-10是0与2^-9的正中点，应选择偶数编码0，当前得到1。
- 紧邻2^-10的下一个较小float32也被错误上舍入；这是先加0.5的
  float32舍入造成的，不只影响精确中点。
- 15/1024应从次正规区进位到编码8，当前截成7。

这组分布是人为覆盖的格式输入，不是模型实际尺度分布；147/33146
不能被解释为模型错误率、触发频率或精度损失比例。

## checkpoint逐专家标量合同

用独立Python脚本直接按safetensors文件头的偏移只读F32标量，
不调用项目loader、框架反量化或现有测试参考。覆盖192个分片，
48层×512专家×3投影×2标量=147456个张量；集合与config/index
精确匹配，类型F32、标量shape、4字节、有限且正值均核对。

- 每个专家gate/up对应的input_scale及weight_scale_2共49152对，
  位模式全部相等，当前loader复用gate标量的假设在此checkpoint成立。
- 各层gate/up的input_scale在该层512个专家间均只有1个值。
- 各层down的input_scale分别有190–301个不同值；不能合并成统一值。

脚本、逐标量值、逐层统计和index/各分片header摘要保存在同证据
目录的audit_scales.py、checkpoint-scale-values.json与
checkpoint-scales.json。未写模型目录，未读取完整权重矩阵，未证明
权重布局、swizzle、GEMM和完整模型的数值正确。

## 下一步参考必须匹配什么

从当前GatherQuant/SwiGLUQuant及MoE GEMM源码读取的流程是：

1. 每16个输入求绝对值最大值，非零组取max/6，零组取1。
2. 除以本专家input_scale，编码为E4M3组尺度。
3. 解码组尺度再乘input_scale，得到实际组尺度；逐元素归一化并
   编码E2M1。实际组尺度为0时当前实现使用归一化系数0。
4. gate/up输出先落BF16，SwiGLU在float32计算，再使用该专家的
   down input_scale量化。不能替换成未量化float32或另一种中间精度。
5. GEMM的alpha包含该投影weight_scale_2×input_scale；组尺度另在
   NVFP4乘法中应用。参考必须避免漏乘或重复乘全局尺度。

独立参考需要同时区分“规范编码”和“当前已知缺陷编码”的结果，
不能默默复制项目转换函数后宣称正确。目前只完成格式参考和标量
合同核对，完整NVFP4块/GEMM/层参考仍未实现。

下一项有判别力的工作是在冻结HTTP输入上确认实际组尺度是否命中
上述错误区间，并定位首次差异；若需增加观测路径，该改动仍必须
先真实HTTP E2E，且观测开销不能混入正式性能数据。没有实际命中与
下游差异证据前，不再盲目换舍入实现，也不认定它解释4K错题。

## 复现格式核对

先确认被测版本已完成现行HTTP门禁；本命令不是前置测试或bench。
所有输出放.q4t-work，不能链接残留候选库：

```bash
mkdir -p .q4t-work/e4m3-contract-check
/usr/local/cuda-13.3/bin/nvcc -std=c++23 -arch=sm_110a \
  -ccbin=g++-14 -Xcompiler=-Wall,-Wextra -Iinclude \
  tools/verify/verify_e4m3_contract.cu \
  -o .q4t-work/e4m3-contract-check/verify
.q4t-work/e4m3-contract-check/verify \
  .q4t-work/e4m3-contract-check/cases.tsv
```

当前接受版本会返回1并报告147条不一致；任何CUDA/文件操作失败
返回2。未来修复仍须先完整E2E，再用此工具验证，不能据此跳过门禁。
