# 当前工作分支主线收敛方案（2026-10-17）

本次仅评估与记录，不实施裁剪、创建分支/标签、PR或合并，不重复测试。
基线main=28a3783350b0c4c3d73fd3d483b5eebe881d2bdb；审查头为
5d5f30bebf43b29866af156cc9256c75237c7862，其后新增本报告不改变审查身份。
共120个未合入提交、1306个差异文件、270240行新增/11行删除。
生产src/include/根CMake无差异，不能将本阶段宣传为新的推理性能成果。

## 推荐边界

建议一个小范围主线交付：验证互斥、已识别工具修复、验收与分支治理规则。
诊断历史保留于远端只读归档，不把所有调查脚本和27万行材料整包合入。
逐文件清单见[evidence/main-convergence-20261017/inventory.json](evidence/main-convergence-20261017/inventory.json)：
7个保留候选文件、2个恢复main文件、6个需精简入口、1291个历史归档候选。
归档候选中可以提取下面指定的最小交付证据，不等于全部删除或无需保存。

### A：值得合入

| 内容 | 价值与依赖 | 接受边界 |
|---|---|---|
| gpu_exclusive.py + test_gpu_exclusive.py | 跨worktree共享锁；退出GPU context竞态、/proc exe消失、setsid后代归属及pidfd身份清理修复；避免已发现的并发污染/自有进程误报 | 保留当前两文件完整身份；不承诺捕获全部图形/瞬态负载，不把ENV_ABORT当候选性能失败 |
| tests/host/CMakeLists.txt | 将guard的现有CPU合同接入host入口 | 仅新增的一个注册项；依赖上述测试及Python标准库，不引入诊断C++编译 |
| profile_http.py + analyze_http_trace.py | 固定实际输入长度，而非把名义204800误作实际204789；检查同prompt重复记录的输入身份 | 保留配套修改；trace只作诊断，不能作为接受计时 |
| AGENTS.md + EVALUATION.md | 独占规则；简化且持平通道；微小差异三出口、有界诊断；少量有效分支、合入后评估留存 | 数值N=4等硬合同不变，不追认NO_GO；分辨带尚未建立，不能宣传已具备自动微小差异验收 |

Guard当前365行、测试323行，只有标准库依赖。显式external lease和
recovery-required门禁虽然由降频调查引入，但本身不写频率，不依赖
clock模块；保留当前已验证完整身份比为了裁减几十行而重构更合适。
README必须同步说明自有后代清理、外部lease退出不释放持锁者的锁，以及
恢复标记阻止后续任务；当前“仅清理进程组”文字不足以描述最终实现。

profile_http的--diagnose-regression仅记录标签，不绕过binary/五档/输出
身份门。其文档中“acceptance”需解释为已有完整HTTP记录，不能误读为
失败候选被批准；本次不扩展到新profiler功能或补全CUDA事件能力。

### B：归档，不留在日常验收路径

- run_acceptance.py和test_run_acceptance.py相对main新增的是ASLR/maps、
  VMM FD传递、UCF/OC客户端包装及对应检查；不存在必须随之保留的
  普通验收缺陷修复。建议整体恢复main字节身份，保留原有正式矩阵功能。
- allocation_address、gather_interval、host_interval、module_load、
  scratch_layout、persistent_layout/backing_owner、ucf_window等probe，
  以及server_address_space、ucf/oc封装、三个gpu_clock模块和专用测试/
  fixtures：都是本次机制调查工具。保持归档可恢复，不维护为默认工具。
  时钟事务虽有恢复合同，也不意味着主线需要新增调频能力。
- HC up、T4 attention、GDN record/fold候选、性能NO_GO和完整因果调查、
  外部项目参考评估/尚未实施计划、原始日志与大批证据：保留完整历史，
  main仅放结论索引和必需的工具验证证据，不把研究计划写成正在实施。
- 分支清理清单、两份未提交补丁及远端归档恢复关系必须可发现；可以在
  main保留短恢复索引，指向不可变归档tag内的完整清单/源码/失败记录。

归档对象不全是无用代码，也不等于数值失败；只是缺少日常主线维护需求。
不采用逐个cherry-pick全部旧提交：多数提交把工具、专题与证据混在一起，
易带回专项入口或遗漏依赖。按最终文件边界收敛更易审查。

## 既有证据如何复用

- guard：初版及context/metadata竞态修复的CPU合同，setsid/pidfd的
  19项CPU合同与真实Nsight自有后代验证，external lease的22项合同，
  recovery marker后的23项合同，以及后续实际连续guard完成记录。
  核对源码SHA/提交与测试范围，不把所有历史数量相加当最终测试数。
- `docs/evidence/hc-up-guard-recovery-20261017/identity.json`明确集成验证
  早于marker修改；normal/kill/restore_failure三个场景只能证明对应
  身份的锁恢复，marker有单独23项记录。保留此限制，不能伪称同一版全过。
- `docs/evidence/hc-up-nsys-ownership-20261017/`保存真实进程归属结果；
  已完成干净trace的输入/输出及导出记录支撑实际输入修正，不使用其
  可能不完整的CUDA事件或污染trace推导性能通过。
- run_acceptance及其测试恢复main后，复用main已通过身份；生产源码和
  build/q4t身份仍须静态核对，不为文档/测试注册或路径整理重跑HTTP。

执行时只提取上述范围的identity、退出/摘要、必要日志和来源索引，
文件路径/依赖应静态闭合；缺少可核对身份则标“未证实”，不得虚构通过。
不能因想补一份漂亮验收报告重新跑已通过测试。

## 有界实施：沿用当前分支，最多三个阶段

1. **先保护完整历史。** 在裁剪前为当时HEAD建立一个annotated远端归档
   标签（如archive/main-convergence-20261017，以执行时唯一名称为准），
   从独立空bare仓库取回，核对commit和恢复清单；不新增工作分支。
   保留全部工作区与本地证据路径，尤其压缩payload恢复映射。
2. **一次成组裁剪、一个可回退提交。** 保留A的工具源码与测试身份，
   B的runner两文件恢复main，其他诊断工具从活动树移除但可从tag恢复。
   将STATUS/PHASES/README/REFERENCE和工具README收敛为当前事实与
   归档入口；原日志完整保存在tag，不修改历史失败含义。主线仅增加
   收敛摘要、规则、最小验证/恢复索引。静态核对每个文件均落入清单、
   不残留诊断导入/CLI文档、不出现失效本地链接、不误删main原有文件。
   不为压低文件数删除必要验收证据，也不重构guard。
3. **一个聚焦PR及合入后收尾。** 本次尚不创建PR或合并。后续PR只描述
   工具和规则，不宣称HC提速/TTFT修复；建议squash合入，避免把120余个
   调查提交及完整证据带入main祖先历史。完整历史由归档tag保留，
   不force-push改写现有分支。合入后核对最终树、归档可恢复性，再评估
   当前工作分支是否删除/复用；不自动覆盖旧工作区或删除证据目录。

“从最终树移除”本身不会缩小merge commit带入的祖先历史；因此本方案
明确推荐squash，而不是误称裁剪后普通merge不会带入调查历史。
远端归档tag仍保存对象，本方案旨在降低主线维护/审查负担，不承诺仓库
对象体积或本地磁盘空间立即下降。

## 出口与停止条件

- 完成：生产实现与main一致；A源码/测试身份可复用；runner回到main；
  专项诊断只在归档；必要记录链接闭合；无未保护独有工作；PR范围清晰。
- 不新增模型/GPU/HTTP、性能采样或通用工具开发；本次也不运行CPU测试。
  将来若裁剪确实改变保留工具逻辑，则该部分不能复用原身份：先停止并
  缩小/撤回改动，或另列具体缺陷与必要验证，不借收敛任务启动回归大循环。
- 最多一次成组裁剪加一次静态问题修正；若仍发现跨层依赖或无法确认
  工具证据，则把对应修复延期归档，保留规则和已闭合工具，不扩诊断。
- 不因收敛把HC因果目标标完成，不放宽数值合同，也不将尚未建立的
  测量分辨带填成任意百分比。旧NO_GO和未决项保持原范围。
