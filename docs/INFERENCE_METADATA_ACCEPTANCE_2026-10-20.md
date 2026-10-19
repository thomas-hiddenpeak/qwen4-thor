# 元数据候选集成验收判定卡

状态：NOT_STARTED_ENV_ABORT。当前质量前置未满足，不启动后续阶段。
本阶段是源所有权结构整理后的质量/性能风险评估，不宣称提速；
已经完成的对象/四层直接合同复用，不再重复。

## 身份与范围

父源码73aab36导出，候选f01e8fa实现。实际二进制分别来自本阶段
parent-build/q4t及build/q4t，驱动启动前保存副本与SHA，所有子阶段前
核验保持。默认build/q4t不改动。MTP代码路径本身未修改，但新增
owner初始化随Model加载，故off/on分别验收，不能只验普通decode。

## 固定预算与顺序

完整run_acceptance_tail.py经gpu_exclusive持锁，输出acceptance-tail-v1，
锁acceptance-tail-lease-v1，全程不释放，不自动重试。

1. 复用现有run_request_cancellation.py跑候选普通decode：max_seq2、
   max_len65536、max_prefill8192，固定20个生成接口请求（含拒绝/取消），
   9个取消控制POST及一个不完整HTTP连接；保留原health/metrics有界轮询。
   health每次等待上限120秒，20ms轮询；RST counters最多100次。
   总阶段上限1800秒，1次模型启动。校验排队取消/FIN/RST/deadline、
   active prefill取消与幸存请求、decode取消、ID复用与后继canary、
   带活跃请求关停。不能将HTTP取消当GPU故障注入。
2. 性能顺序固定parent-off、candidate-off、candidate-on、parent-on。
   每服务五档1024/4096/8192/45056/204800，各3条固定请求，总60条、
   4次模型启动，每服务7200秒上限。tools/evalscope HTTP E2E，max_seq1、
   max_prefill8192、max_len208896，greedy256输出，fixture来自B1b
   candidate-off的固定性能记录副本。startup300秒，port8740。
   完整保存三个样本，不按文件整理或观察方向补采样。

## 合同、指标与出口

正确性：HTTP输出/长度/确定性合同必须通过；同模式父候选prompt与
输出摘要逐档一致。取消合同失败停止，不继续矩阵。数值/质量失败
不能用计时解释，先保留并定位具体失败。监控失败/竞争ENV_ABORT。

全部TTFT与decode_tps原值公开，首请求分列，两个模式分别比较，
不以平均收益抵消单档回退。报告中位数百分比只作为观察值。
分辨带：NOT_ESTABLISHED；现有三次样本不能估计真实回退上界，
跨启动顺序效应不能由本轮自动排除；没有随意填入0.5%/1%。
因此统计性能结论默认INSUFFICIENT_EVIDENCE，不自动宣称提速、持平
或非劣；有明确可归因回退则拒绝/修复，不能因缺分辨带而忽略。

本候选增加上传owner/event，不是仅删除重复计算/元数据获取/同步，
不得直接套用B1a/B1b“有限工程接受”许可。完整证据形成后明确决定
保留候选、回退或请求用户认可具体风险；提交/推送不构成接受。
默认部署保持。采样预算用尽不追加校准或追求有利结果。

前置quality-v1现为ENV_ABORT，完整质量PASS未满足，本计划未执行。
见[中断与关停封存审计](evidence/inference-metadata-20261020/quality-v1/audit.json)。

## ENV_ABORT后的有界恢复与封存修正

内核journal同一分钟存在tegra I2C timeout，但时间不同，无法据此
证明nvidia-smi超时原因。当前空闲状态一次查询及随后5次固定查询均
在5秒限制内成功、无compute PID；仅证明环境当前恢复可用，不声称
监控根因已修复或高负载下必然可靠。gpu_exclusive不改、timeout仍5秒，
再次监控失败仍立即ENV_ABORT，不忽略轮询空档。

已确认的工具缺陷是中断时manifest生成早于子进程最终清理。
新增seal_stage外层在gpu_exclusive返回（它已核对自有后代清理）后
再生成sealed-files；中断驱动原manifest原样保留，不回写旧数据。
源码Python语法离线检查，实际封存结果在本次阶段完成后核对。

恢复预算固定仅MTP质量11条、1次模型加载，无自动重试；候选binary、
题目及参考与quality-v1完全相同。off11条已在首次监控失败前正常完成，
保留其输出及当时有效监控区间，不重复普通decode；其来源仍是最终
ENV_ABORT阶段，最终报告明确这一局限，不改原lease终态。

命令：seal_stage.py --output .q4t-work/inference-metadata-20261020/quality-on-v2
-- python3 -B tools/verify/inference_metadata/run_quality_on.py。
外层调用gpu_exclusive持完整阶段锁，输出目录内lease。质量1800秒，
模型加载300秒；再失败不追加同类样本，转具体环境/失败分析。
这是环境恢复确认及封存工具修正后的单次受影响组重验，不重跑整套。

后续验收驱动已改为显式核对两组证据：off仍引用原quality-v1，核对
其原manifest中全部off文件、11条结果、正常server_after_shutdown及
覆盖起止时间的无冲突monitor记录；on必须为quality-on-v2的PASS且
外层guard COMPLETED，全部sealed-files摘要一致。两组binary SHA
须相同。保留原lease的ENV_ABORT与off复用局限，不拼装新“完整旧
lease通过”记录。后续阶段尚未启动。

## 单次恢复终态（2026-10-20）

quality-on-v2于UTC 2026-10-19 17:20:33退出75 ENV_ABORT，再次为
nvidia-smi PID查询5秒超时。最后一条200K请求在关停时取消，不能将
已完成的前10条拼成11条通过。run-state为INTERRUPTED；guard终态
优先。seal_stage在清理完成后封存，全部摘要复核一致，修复后的
封存机制本次有效。证据见[evidence](evidence/inference-metadata-20261020/quality-on-v2/audit.json)。

单次恢复预算已用尽，不再追加采样，不启动取消/五档，不放宽监控。
本时段kernel记录多次tegra I2C timeout，但没有堆栈或驱动因果证据，
不能确定其导致PID查询超时，也不能归因于上传候选。候选未验收，
默认binary保持；继续不加载模型的代码/环境诊断。

容量审计随后补入B<=max_prefill拒绝，新的源码尚未重建q4t；bounds
目标已独立验证。旧tail即使能核验旧binary，也不能作为新源码验收。
再次执行前必须重新冻结源码/binary身份和受影响合同，不能把当前
build目录内新静态库与旧q4t混为同一身份。

## 单次环境诊断

状态：SINGLE_DIAGNOSTIC_COMPLETED。用户2026-10-20答复“可以继续”，
授权前述1加载/1请求的有界诊断；已完成，不扩展为完整验收重跑。

已准备run_monitor_diagnostic.py，复用原候选保存副本和原on组唯一
204800-token夹具：模型加载1次、MTP HTTP请求1条、输出上限32，
startup300秒包含在驱动900秒上限内；guard最终排空另计，无重试。
不跑其他10条题目、不跑取消/性能矩阵，不修改监控5秒阈值。外层
seal_stage调用原gpu_exclusive持锁，输出monitor-diagnostic-v1。
旁路仅每0.2秒读取guard直接子进程nvidia-smi的/proc状态、wchan、
syscall及stack，最多4500轮；权限不足原样记错误，不提权、不安装
工具、不额外调用NVIDIA API。观察本身可能扰动计时，不作性能结论。
新证据只能帮助判断查询等待位置；一次成功不证明负载下监控可靠。

本次授权命令：

```bash
python3 -B tools/verify/inference_metadata/seal_stage.py \
  --output .q4t-work/inference-metadata-20261020/monitor-diagnostic-v1 \
  -- python3 -B tools/verify/inference_metadata/run_monitor_diagnostic.py
```

旧候选已另存candidate-before-capacity-q4t并核对原质量SHA；容量修复后
完整候选q4t已构建零警告，仅构建未运行。身份见
[构建证据](evidence/inference-metadata-20261020/capacity-build-v2/identity.json)。
旧四层/HTTP证据不改标新源码通过。默认build/q4t保持不变。

### 授权后的准备失败修复

monitor-diagnostic-v1在runner准备阶段因旧binary旁缺少CMakeCache.txt
失败；events仅有runner_start，无server_started，实际加载/请求0。
保留其COMMAND_FAILED及封存，不解释为负载监控失败。原质量off的
CMakeCache通过原manifest摘要核对，与原binary一并放入新输出目录。
修正驱动输出monitor-diagnostic-v2，执行尚未消耗的1加载/1请求预算，
不增加GPU样本次数，不覆盖v1。旧cache保留真实历史构建路径，不伪造
为新源码构建信息。本次以旧binary SHA为执行身份。

### 单次诊断结果

v2 guard COMPLETED/exit0，请求actual_input=204800、输出711273、
exact_match/length_match通过、finish=stop，服务正常关停；v1实际
加载0，v2加载1，请求总计1，未增加样本。v1/v2封存分别14/43文件
复核通过。原始和派生证据见[记录](evidence/inference-metadata-20261020/monitor-diagnostic/result.json)。

本次未复现查询超时。旁路仅捕获5个不同PID：wchan为3次
hrtimer_nanosleep、2次0；syscall均EPERM，stack四次EACCES、一次
进程已结束。未提权或改系统ptrace权限。kernel仅记录5次对应/proc
访问的ptrace权限检查，不能据其文本称实际attach成功；该时间窗未见
前次I2C timeout，但既不能证明I2C为根因，也不能证明问题已解决。

这次是冷启动后的单条请求，前次为连续质量组，运行历史不同。
不能拼入旧中断组宣布完整通过，也不能代表容量修复后的新binary。
单次新授权已完成，取消/五档仍未启动；不追加诊断或计时样本。
下一项仍是新源码运行时验收的明确预算/身份与最终候选去留决定，
当前没有全模型质量或性能接受结论。
