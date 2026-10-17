# 工具与验收规则主线收敛（2026-10-17）

生产基线main为28a3783350b0c4c3d73fd3d483b5eebe881d2bdb；生产src/include/
根CMake未改。本次交付只有验证工具、验收/分支规则和必要索引，不是
HC up提速或TTFT根因修复。沿用原分支，PR建议squash，不自动合并。

## 阶段与交付边界

1. 已为完整历史9a5bc0c建立远端annotated tag
   `archive/main-convergence-20261017`，从独立空bare仓库取回核对commit、
   清单和未提交源码归档。阶段提交ca81858；
   [恢复记录](evidence/main-convergence-20261017/archive-recovery.json)。
2. 成组裁剪保留gpu_exclusive及其测试、host注册、profile/analyzer实际
   输入修复、AGENTS/EVALUATION规则。工具源码与测试保留原身份；
   run_acceptance及其测试恢复main字节。移出ASLR/UCF/OC/VMM/调频等
   专项诊断，不改生产、N=4、确定性/状态合同；不重跑任何测试。
3. 创建聚焦主线PR后记录链接。建议squash，只把最终范围带入main；
   不force-push旧分支，完整调查历史由tag保留。合入后另评估当前分支。

[裁剪前方案与逐文件清单](ARCHIVED_RESEARCH.md)永久可恢复。本地1248份
被移出Git活动树的证据/清单仍在原路径，由本仓库info/exclude隐藏；
不是删除磁盘证据，也不更改项目全局ignore规则。全部工作区路径保留。
新克隆通过归档tag取得历史；此操作不承诺Git对象或磁盘空间缩小。

## 已有证据复用及限制

[交付身份](evidence/main-convergence-20261017/delivery-identity.json)记录当前
工具SHA及来源提交。此次只进行静态身份/链接/范围核对，不构建、不
运行CPU/GPU/HTTP测试、采样或诊断checker，也不把整理称作重新验收。

- guard的context退出及/proc exe消失竞态、setsid后代归属修复已有
  分阶段CPU合同；最终marker版本历史记录23项通过，之后源码/测试
  未改。数量不累加为一次完整测试。来源为归档日志与提交ae24a6e。
- [旧监管集成身份](evidence/hc-up-guard-recovery-20261017/identity.json)
  明确集成早于marker；[三场景结果](evidence/hc-up-guard-recovery-20261017/result.json)
  只证明对应版本normal/kill/restore_failure锁恢复，不能伪称最终版
  一次全过。external lease与marker本身无调频依赖，保留当前完整
  身份，不为缩减几行重构。工具只用Python标准库。
- [真实Nsight后代归属](evidence/hc-up-nsys-ownership-20261017/ancestry.json)
  和[guard退出](evidence/hc-up-nsys-ownership-20261017/lease/exit.json)
  保留原始记录；完整probe/日志见归档。轮询不证明没有所有图形/瞬态干扰。
- profile/analyzer修复名义204800与实际204789混淆；复用归档中的
  已完成HTTP输入/输出/trace导出证据。已知某些trace含污染或事件
  完整性警告，不用计时证明性能或因果；诊断标签不豁免已有身份门。
- runner回到main，复用原身份；生产binary仍be74946d…，既有模型
  数值/HTTP证据未因文档裁剪失效，不启动完整推理验证。

保留“复杂度下降且性能持平”的通道、微小差异三出口和诊断预算。
各指标可信测量分辨带尚未建立：规则不是自动判定器，不虚填容忍率，
不追认HC、GDN或其他旧NO_GO。原HC因果目标仍未完成，不自动重开。

若静态核对暴露工具逻辑必须改变，应停止/缩小该部分范围，不能为
本次整理扩展测试或调查。保留修复均未做这种逻辑改变。
