# Hyperconnection残差及层间交接（2026-09-23）

## 结论

原4K请求48层，取prefill末token和首个pooled decode。attention
融合写回/分组归一化与MLP独立写回分别核对，共192次、1966080个
BF16残差输出，同FP64参考舍入，也同CPU独立fma后BF16舍入。
实际812条边界逐字节一致；与上一轮1872份BF16生产快照相同。
生产未改，三侧HTTP仍600440（应360284），原质量失败未修复。

这里确认的是已捕获注入门、子层输出及残差条件下的写回与交接。
不是独立生成注入门或混合权重，也不是完整hyperconnection正确。

## 观测归属的两次失败

首次只按GroupedRmsNormKernel短名识别，PLE/MTP/HC同名核造成
句柄覆盖。实际406份而非480份，完整性门禁失败；before/observed
HTTP保持原错、服务0，after未运行，也未做低层数值分析。
接受二进制的设备stub明确包含不同源文件标识，修订限定
hyperconnection_cu。初版失败数据与诊断保留在hc-handoff目录。

v2三侧HTTP及480份计数通过，但身份检查发现decode read错位。
prefill输出头只取最后一行，另执行一次T1最终混合，旧逻辑误将它
当成第0层decode读。错误归属的数据逐字节等于第47层prefill
输出，后两份分别等于实际decode第0/1层残差。源码model.cu的
LogitsRows::kLastRow→HeadForward、model_head.cu的mixer调用与
此证据吻合。v2身份分析退出1，CPU两类combine重算退出0；不能用
该轮计数通过或combine一致宣称跨层身份通过，原始数据保留。

v3捕获prefill第0层实际HC norm权重指针，以其首次T1调用确定
decode起点，排除最终输出头混合。再次零警告构建后首项三侧HTTP，
三次原错600440、4096输入/7输出、stop，三服务0、质量驱动1。
480份完整之后分析身份，812条全部通过。两次重跑用于修复观测，
不属于重采样寻找质量通过；没有修改生产实现或舍弃失败证据。

## 覆盖与计算

每阶段48份read、48份attention融合写回、48份MLP写回，及96份
attention/MLP混合，共480份。核对hc=4、hs=2560，取实际输入、
输出和注入门；归一化/混合输出本轮作为现场边界值，不独立重算。

812条身份包括：

- read的trunk同attention写回所保留残差；归一化输出同混合输入。
- attention写回结果同MLP所保留残差；融合归一化同MLP混合输入。
- 全48层MLP混合输出/写回block输入，分别同此前MoE输入/输出。
- 36个linear层attention混合输出/写回block输入，分别同实际输入
  投影输入/输出投影结果。12个full-attention内部边界另待覆盖。
- 每阶段46条无PLE干预的相邻层写回→下一层trunk逐字节相同。

第0层输出到第1层read之间有PLE添加，两阶段均不同且单列保留。
它不是恒等交接，不通过放宽数值误差处理；PLE运算仍未独立证明。
第0层trunk来源和最终输出头也不在本轮证明范围。

残差参考为R[b,c]+block[c]*inject_gate[b]。FP64参考用实际BF16
三个操作数，先转FP32再最近偶数舍入到BF16。另用CPU libm fma
显式单次FP32融合乘加、BF16最近偶数舍入，编译关闭隐式收缩；
192次1966080输出全部位同，原始结果文件逐字节复核。注入门的
生成、分组归一化、低秩投影及混合sigmoid仍待独立核对。

## 证据与下一步

证据在`.q4t-work/e2e/hc-handoff-v3-20260923/`：
raw-http-audit.json、handoff-reference.json、cpu-reference.json、
previous-binding.json、artifact-binding.json和observed-captures/。
无版本与v2目录保留observer-diagnosis.json、失败数据及终止记录。
生产二进制仍2392f6b1，MTP关闭；诊断时间不作性能验收。

下一步建立HC norm、mix_down/up、inject投影与门控的独立参考，
优先绑定实际权重和输入；随后继续完整prefill GDN状态、QSA、PLE
与输出头。当前局部一致不能作为原4K错误与实现无关的证据。

## 后续：混合sigmoid与四分支加权参考

复用已完成三侧HTTP及身份验证的v3冻结快照，未改观测或生产实现，
未另跑模型。读取并校验上一轮artifact-binding全部条目；本轮192组
up/normed/output共576份输入文件分别记录SHA，分析后再次校验不变。

48层×prefill末token/首decode×attention/MLP，共192次、491520
个BF16混合输出。数学参考为四分支sigmoid(up)*normed之和除以4，
使用FP64后经FP32→BF16最近偶数舍入，有8项不同；索引及参考值保留。

独立CPU参考按分支0/1/2/3顺序执行显式FP32 fma，除4再BF16舍入，
关闭隐式浮点收缩。CPU sigmoid使用double exp舍入FP32、FP32加法与
除法，有6项不同：L30 decode attention/1940，L35 decode MLP/1360，
L36 prefill MLP/1170，L37 decode attention/1152，L42 decode MLP/1216，
L45 decode MLP/2409。没有改变容忍阈值或删除这些差异。

设备只从实际up值重建1/(1+__expf(-up))，不调用生产MixGatePair。
将其结果交给同一个CPU四分支fma累加器后，491520个BF16全部位同。
所以本次6项纯CPU差异可由sigmoid算术来源解释；FP648项包含不同
精度、累加和非线性共同影响，不逐项仅归因为exp。不是完全独立于
CUDA数学近似的证明，也不是up/normed来源正确的证明。

两份工具零警告构建、运行退出0；控制器逐文件复核差异计数并保存
全部输出。证据目录`.q4t-work/e2e/hc-mix-reference-20260923/`中的
input-binding.json、fp64-reference.json、mix-reference.json及
artifact-binding.json；原HTTP质量失败仍明确保留。下一步绑定实际
HC norm权重，核对归一化与mix_down/up、inject投影及注入门生成。
