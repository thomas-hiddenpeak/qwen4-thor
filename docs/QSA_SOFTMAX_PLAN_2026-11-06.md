# 大T QSA softmax并行化：有界性能判定卡

2026-11-06用户授权：先实现大T sparse attention softmax并行化，保持
原数值合同，先有界HTTP验证，收益达标后才完整验收。本项为性能优化，
严格遵循EVALUATION及PERFORMANCE_DECISION_PROTOCOL，不使用bench。

## 身份、机制与合同

复用codex/mtp-selected-head-rows；研究父fdb2a66b的运行来源为5d641ea4，
已完成R1/R2短验，父ELF aae93a73。相对该父只改full_attention.cu。
R1/R2层名兼容及诊断保留，A父/候选均关闭观察；不混入旧GDN、末行
head或其他客户端改动。当前默认c1bdb0a7/sidecar0efc2c0d另行保护；
A是与已短验父的增量比较，完整B还须与当前接受默认比较。

仅generic sparse的T>8启用模板true；T<=8原split decode和显式
ablation的generic false保持。大T含非ragged/pooled等原有布局，不能
将T>8等同于纯prefill。选中位置、paged KV、GQA 24Q/2KV、hd256、
16位置tile、QK/PV MMA、cp.async缓冲、gate及最终舍入均保持。

最大值仍每头一线程按p递增遍历；alpha仍expf(m_old-mx)。独立pr由
192个(head,position)lane计算，同时保存原FP32值及BF16 sP；随后
每头一线程从原sSum*alpha开始，按p=0..15累加FP32 pr（含零尾）。
不采用树形归约，不用BF16概率反算分母，不扩大tile或改exp函数。
新增768字节shared及两次CTA同步可能抵消exp并行收益；源码运算顺序
不能替代编译后逐位验证，特别是FMA融合、subnormal和边界输入。

## A：第一项测试为真实HTTP

先必要零警告构建；此前不执行单测、CUDA数值/微测、bench或profile。
两启动顺序固定父→候选，MTP关闭，单流、prefill8192、max_len208896、
startup-plan off。沿用既有输入，1K×3、45K×3各256输出，随后11个
五档固定质量题、每题最多32输出。总额**2启动/34HTTP/1200秒**，
工作窗口1140秒、每启动≤570秒，留60秒清理；无warmup/连接生成
探针/重试，不清系统页缓存。整个驱动同一GPU租约，保留首请求原值。

投入门槛：45K三请求**平均TTFT至少下降3%**，且aggregate E2E下降；
父/候选prompt、文本、输入/输出计数和finish逐请求一致，22项固定
质量题均通过。普通decode不要求MTP直方图，必须证明MTP未加载。
1K TTFT/decode/E2E和45K decode分别记录，A不据此宣布五档保护项已
通过。3%是本次继续投入门槛，不是损失容差或统计非劣界限。

任何环境/监控失败记ENV_ABORT并保留现场；质量/输出失败则保留首
失败定位。有效数据未达到收益门则NO_GO_FEASIBILITY_TARGET，结束
本轮，保留默认及候选，不追加采样/参数扫描，不进入B。A通过仍不是
性能接受或默认部署许可。

## B：仅A通过后冻结细节并执行

完整B总额≤7200秒、8次模型启动、144HTTP；直接GPU合同≤900秒，
不做长稳。先冻结实际子清单再运行，已有证据只有身份/范围适用才复用。

- 当前接受默认与候选：plain及MTP完整/subset三模式，五档每档×3；
  同模式逐档TTFT/decode/E2E保护、质量和输出合同，首请求分别记录。
  A父非当前默认，不将其plain数据自动当默认五档证据。
- 逐位独立比较父原kernel与候选真实kernel，包含T=8/9/12/大T路由、
  nsel=0/1/15/16/17/2048/2049/2051、非顺序页、pooled序列、有限
  边界/零/subnormal/抵消、尾部及canary。另核对softmax FP32分母/
  BF16概率与原顺序参考；不以最终语义分数掩盖计算差异。
- 适用prefill、多槽、MTP接受/恢复、状态缓存的直接合同与HTTP质量；
  新协议的直接工具闭环。旧托管包仍固定旧身份，不自动迁移或扩大
  客户端工业化。主要性能目标仍长上下文TTFT，不能用平均收益抵消
  保护项问题，也不因微小波动反复补样。

全部满足后才按已有授权收尾交付，另最多2启动/6HTTP/900秒用于
可核验回退包、默认与元数据及部署健康。否则仅研究收尾。R1/R2与
softmax各有提交边界，不整体合并研究历史。
