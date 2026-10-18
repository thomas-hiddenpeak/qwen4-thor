# MTP extend gather offset 持久化 B1b

2026-10-19，IMPLEMENTING，尚未验收。仅处理 extend gather offset。

## 执行前冻结卡

基线 main a893dc8，运行时含已接受 B1a；默认 binary SHA256
44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03。
工作区起点干净，无现存工作分支；建立唯一 codex/mtp-gather-b1b 以隔离
未接受候选，保留 main 与默认 binary。原二进制独立复制至
.q4t-work/b1b-20261019/q4t-baseline。候选与测试身份在执行前另存。

路线为局部简化的有限工程接受申请；最终必须另获负责人对 B1b 具体计时
不确定性的认可。统计分辨带与可信差值区间未建立，统计结论只能为证据
不足，不自动宣布持平/提速，不继承 B1a 接受决定。

- 不改 offset 映射、T_ext、上传、gather kernel、位置最大值下传、状态
  恢复/输出或空 extend。增加 max_seq*k_max int scratch（含单流），
  分配失败清空、释放复位，容量/指针检查先于上传和使用。
- host gather_off 在函数作用域存活到既有 cleanup drain；正常/失败退出
  都先 drain，再销毁 host vector；设备缓冲只在下一次串行 step 复用。
- 预算器目前只支持 MTP max_seq=1；默认 k3 将16字节从临时设备项迁到
  持久 scratch，host六组行数组计费不变。运行峰值不变，加载峰值+16。
- 零警告 Release 构建后首项测试为候选 HTTP quality-on 11请求。
  随后 candidate-on 15、quality-off 11、candidate-off 15、parent-off 15、
  parent-on 15。总计82请求、6次服务启动；失败停止，无重试/连接探测。
  单流容量1/8192/208896，greedy，k3，固定snap-records五档×3、输出256。
  使用既有 numerical-closure 固定输入/输出参考，并核对 B1a 同输出证据。
- 两模式各档分别报告平均TTFT及总decode token/总decode时间，正差表示
  变慢；首请求单列，不能以其他档收益抵消回退。原始DB/响应全部保留。
- HTTP输出/质量/长度/重复性检查后执行生命周期、预算、B1a位置合同及
  同形状完整multi step父/候选差分；覆盖连续复用、最大extend、a=0/1/2/3、
  混合/全部终止、非单调槽号及失败退出。直接测试夹具冻结后各执行一次，
  不以改阈值接受失败；具体缺陷修复只能重验受影响项并保留首次失败。
- GPU完整阶段持同一 gpu_exclusive lease；竞争/监控失败ENV_ABORT，仅停
  自己的进程。HTTP总阶段上限4小时，直接合同上限2小时；不自动补采样。
- 只允许一次离线源码/日志分析，0额外请求，最多两个机制假设：缓冲
  生命周期/映射错误；移除同步后的执行/测量变化。没有区分证据即停止。
  已确认计算错误/可归因退化否决；否则带完整风险申请工程接受或保留未决。

## 证据复用

B1a 原 HTTP 两模式质量与 AB60 输出可作父身份参考；本阶段另采同期五档。
PLE所有权、独立accepted-prefix/checkpoint/prefill与完整模型N=4合同实现
未改，复用 numerical-closure 原证据及其局部范围；不重跑完整256步诊断。
B1a 精确位置直接合同重新运行以保护接入。尾部/取消的控制流没有变化，
复用 B1a 已通过8请求；新混合/全终止及连续复用直接合同验证受影响路径。

## 回退

候选独立构建在 .q4t-work/b1b-20261019/build，默认build/q4t不覆盖。
回退仅撤销B1b实现/预算/测试提交，保留B1a及全部证据；不修改模型/reference。
提交/推送仅发布可审查阶段，不代表接受、合入或部署。后续Megatron不启动。

直接夹具补充（采样前）：父参考使用git show a893dc8:src/mtp/mtp.cu原文，
针对新增字段后的同一header编译，链接其余相同推理对象；仅禁用父夹具的
“新增offset必须非空”断言。父实现不使用新增字段，不用于计时。8场景共
12步的完整有效输出、verify/extend中间量、主模型recurrent及draft全KV/
raw/comp原始字节必须一致；模型为两层主干＋真实draft，槽序1,0。k2与k3
零head各连续3步覆盖a2/a3最大extend；自然/首接受与k1覆盖a0/a1；混合及
全部终止覆盖跳过。该局部同形状差分不声称全模型任意输入证明。
新增指针/容量不足场景检查失败输出无效化及返回时stream已drain；原
lifecycle在max_seq=1/3对所有分配边界注入失败并重试。所有预算合同重验。
父参考构建脚本首次遗漏nvcc host参数转发，构建失败已保留；补齐与CMake
一致的-forward-unknown-to-host-compiler后构建，不属于GPU测试或采样。

HTTP期间的夹具审查发现ModelSnapshotState是单槽helper，不能覆盖第二槽。
直接测试尚未执行，改成显式读取两槽ssm/conv/PLE。保留初始测试binary与
identity；direct_build_gate仅在http-complete后编译测试/父参考并冻结
最终direct-identity，然后执行原清单。全部HTTP计时期间不构建，生产
candidate SHA不变，HTTP预算与直接场景数量不变，不产生补采样。
