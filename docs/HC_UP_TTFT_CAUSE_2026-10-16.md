# HC up TTFT差异定位（2026-10-16）

用户授权继续定位，不重复采样挑选结果。此前NO_GO及三对差值保留。
生产代码/main及数值合同不变。此阶段是已有失败定位，不是新性能候选。

## 阶段1：静态核对与诊断计划（执行前冻结）

- A=be74946d…原main，B=f01a0ee6…原HC up，使用保存二进制。
- 原22份CUDA ELF相同，新增HC内核不在200K off形状分派内。
- host符号1046个公共函数地址全部改变，只有Bf16SmallMGemm大小变化
  （1040→1176字节），新增HC wrapper/stub及链接器thunk。
  大小一致不等于指令一致，地址/布局仍是待排查因素，不视为根因。
- 固定A、B各一条200K off真实HTTP CUDA trace（共2条），模型加载后
  才开始采集；同原prompt、256输出及容量，不追加TTFT重复测量。
- 比较prefill kernel序列/配置/次数，GPU时间与空隙、拷贝及CUDA API；
  同步只读记录GPU/CPU频率、温度。trace本身会扰动耗时，不能替代
  原E2E或据此撤销失败。若机制未能定位，明确未决，不声称查清根因。
- 诊断工具修正档位长度与实际输入混淆：204800档实际204789，严格
  对照归档responses输入身份；trace分析同样按实际输入核验embedding。
  增加显式诊断标签，不改变任何模型算法、精度或验收阈值。

原始证据目录：.q4t-work/hc-up-cause-20261016/。

## 阶段2：trace结果与下一步定向诊断

A/B各一条均输出一致、服务和export退出0。两侧prefill共2264679次
kernel，四条stream内完整名称/启动配置序列hash全部一致，新HC为0次。
全局开始时间序列不同是并行交错，不是各stream执行工作改变。
CUDA API次数亦相同；本次647次频率观测GPU1575MHz、内存4266MHz、
CPU2601MHz不变，GPU温度44.25–73.375°C。不能据此追溯历史温度。

A的GPU busy158.968s、gap95.525s；B为158.370s、1.897s。
A trace出现cudaMallocAsync最长28.131s阻塞，其他多次秒级阻塞；
B无同样现象。此差异远大于原89ms，不能拿它解释历史回退或反判B更快。
trace只证实了执行身份，未证明原TTFT差异的根因。

针对具体trace失真，冻结第二种观测：不使用Nsight，A/B各一条相同
200K off HTTP，开启二进制已有Q4T_LAYER_TIMING；只以LD_PRELOAD记录
cudaMallocAsync原调用前后单调时钟/size/返回码，不替换返回值、不插入
CUDA同步、不改变计算。日志开销及event同步仍存在，仅用于诊断。
固定2条，不追加同类样本，不改验收规则。对比25块事件时间、分配阻塞
及HTTP边界；保留失真trace，不能将两个观测阶段混合成性能统计。
