# Mirror 保留与替换历史 — 固定八请求离线研究

更新：2026-10-06。承接69e2d04，独立分支
`codex/offload-mirror-retention-20261006`。本阶段只复用上一轮新observer-on
两服务八请求，零新增HTTP/模型运行，不更改runtime或原观测开关。

## 问题与源码事实

上一轮主比较k1的mirror缺口468=累计plan入口机会差470−直接抢占差2。
这不能直接说明入口差来自需求组成，还是同一需求上的保留历史。
新假设是：mirror里已经被GPU/L2覆盖的副本占位，可能导致仍有独立供给
价值的镜像更早被覆盖。仅见冗余占位不足以证明覆盖发生或可省READ。

代码实际查找顺序是L2→mirror→软件READ（moe_residency.cpp:633–660）；
header里先ring后L2的旧注释不能作行为依据。mirror命中不消费映射，也不
刷新cursor；写回从cursor找首个event就绪、未claimed的槽，不优先空槽
或GPU/L2副本。成功publication替换映射并清除同expert旧副本。
真正单token forward入口同步既有stream；prefill内部singleton子块
不能套用同样的事件完成前提。GPU规划顺序不等于worker写回顺序。

## 冻结范围与输出

[冻结清单](evidence/offload-mirror-retention-20261006/scope-plan.json)
SHA256为`7b611ebf65f194f166d8d9e1635d88f5eced5e790d8fe38012fb754beff7e089`。
固定`s02-as-on`与`s03-al-on`各k0–k3，48层、255个true decode forward，
共97920层计划；保留k0输入不同和k2/k3方向反转。旧16请求不池化。

- 对每个已捕获的请求入口、prefill末端、decode末端，互斥统计mirror的
  GPU-only、L2-only、both、sole和空槽；不插值未观测状态。
- 从每条实际decode入口回放GPU missing/victim计划，核对四个decode
  前缀计数与末端完整GPU状态，不更改策略，不重新模拟prefill。
- 对每次miss记录入口mirror及严格更早GPU victim的潜在供给支持，以及
  最近victim的plan距离、中间victim数。这不是实际mirror寿命。
- 对齐S/L，分别统计共同miss、S独有和L独有miss；先查ordered路由是否
  相同，再解释差异。路由不同不当作受控历史比较。
- 结合原观测逐层累计candidate数，给共同miss上候选差的保守区间；
  不把470拆成独立因果百分比。

可能供给集合只取实际decode入口mirror与严格更早的GPU victims之并集，
不裁成最后8次驱逐。每plan候选最多8；共同和独有的上界分别累计。
给定某side/layer实际候选总数C，有
`X_common ∈ [max(0,C−U_only), min(C,U_common)]`。
必须检查C不超过总支持上界、区间非空；S/L区间相减后逐层求和。
这是可行集合的保守外包络，丢弃约束可能变宽，不宣称精确可达。

## 决策出口

若k1同输入token序列、长度且ordered路由一致，共同miss候选差下界严格为正，可排除
“只由当期miss集合组成差解释全部缺口”。历史GPU需求和写回依然可能
改变共同miss的可用性，不能由此证明冗余副本挤掉了sole镜像。

只有同时在k1的decode入口至少一个同层ring观察到GPU-covered与sole
镜像共存，才提名一项未来GPU冗余mirror优先回收规则供考虑。该条件是
原scope端点条件的收紧，不使用L2-only占位单独触发GPU回收候选。
否则报告需求/保留不可识别，不能凭端点冗余直接选择策略。

本阶段不自动增加观测，不试第二规则。缺失的每plan mirror集合、
claim/实际reserve可用槽及publication/EventQuery顺序明确列为未知。
未来使用信息只用于机会/上界，不能偷渡为运行时可用策略。
提速实现另冻HTTP首测及五档/目标/history/资源验收。

## 执行与保护

工具、输入投影和独立静审已完成；26项核心与19项runner合同首批45/45
通过，实际trace分析尚未执行。输入投影3,941,749B，SHA256为
`f3831ccc88e061aea3141f23f581be9c963da827ee3ded7761509d501c73dcb3`。
首测前补齐精确输入token摘要一致门，保留静态发现；scope组装的相对路径
预检错误亦保留并在冻结前修正，未触发测试/推理。干净工具提交与执行账
冻结后只做一次固定八请求分析；首次失败保留，仅修复重验受影响项。旧GPU55、supply41、observer46及HTTP/资源证据按未变
身份和原范围复用，不算本轮新增通过。

MAIN七项修改、完整diff与binary已记录入口保护；原交付工作树不改。
模型仅228项文件元数据和config/index摘要，审计不读payload；reference
只读。旧NO_GO、默认关闭、GEMM/精度/预算、MTP/Phase D与整体54GB未知保持。
