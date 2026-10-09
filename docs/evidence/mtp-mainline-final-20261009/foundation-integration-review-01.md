# foundation 合并准备独立审查（2026-10-09）

只读审查；没有修改源码、index、分支或运行模型。逐文件 blob、SHA256、
祖先关系与当前冲突文件身份见同名 JSON。

## 结论

在 M2 实现/验证形成阶段提交后，将 foundation `5aacc9f225056b47fca766dcf9d49a170e60978f`
合并进当前 research 分支即可一次保留全部独有成果。不要 rebase、cherry-pick
重复实现或修改 main。CMake 和所有 M2 运行/测试工具保留 research 当前字节，
本次合并只有历史材料和导航变化，不需要为此重新构建或重复模型请求。
这只是审查准备，未执行合并，也不代表主线 Goal 完成。

## 两个非祖先提交的精确范围

共同祖先 `c365b9b247c93571bda1f95551a9424dce15f99b`。foundation 独有：

1. `92d1d95aa69e98413d054e7c107f79f828ee9f3e`：21 路径。
   CMake 的唯一实现性改动是从默认 q4t_tests 移出原跨模式失败诊断，
   新增同源、原断言的 EXCLUDE_FROM_ALL 目标。其余为独立交付报告、
   15 文件原始证据/复核脚本包、状态、文档索引、日志及日志索引。
2. `5aacc9f225056b47fca766dcf9d49a170e60978f`：只改 README、STATUS、
   2026-10-08 日志，删除旧 CLI MTP 推荐并说明当时实验范围。

从共同祖先至 foundation 的 include/src/cmake/tests/tools 无任何变化。
实际资源/RoPE/checkpoint 修复提交 f02e75e、0284b60、ec0ae6f 和最终
c365b9b 均已是 research 90d5f539 的祖先。因此没有遗漏的独有运行修复
需要带入，不应为了建立依赖重复应用这些改动。

## 六处冲突的最小解决规则

这里“research”指合并前最新 M2 阶段提交，不是把工作树退回90d5f539。
当前预览树 04ca76f3e059e51d22b21c1a5f0204ae5adb4a33 只用于定位冲突；
真正合并后需再次确认冲突列表。

| 路径 | 解决方式 |
|---|---|
| CMakeLists.txt | 整文件保留 research M2 原字节。cross_mode_diagnostic 的功能已完整包含，并保留最新 copy/fault/terminal 目标、修正后的 raw symbol 和其他研究诊断隔离。foundation 只剩不同注释，无额外功能需拼入。 |
| README.md | 保留 research 的 generate 拒绝、默认 sequential、显式 T4 实验及成本/支持配置说明。在现有验收链接附近只补“基础修复历史及独立证据见 [MTP基础修复交付](docs/MTP_FOUNDATION_2026-10-08.md)”。不能恢复“所有 serve --mtp 仍实验”的过期入口结论。 |
| docs/README.md | 保留现有全部行，新增 foundation 报告一行；职责为基础修复包、已知跨模式失败及原证据复用边界，时间标明 2026-10-08 历史交付。 |
| docs/STATUS.md | 保留最新 M2/Goal 当前状态。现有 A/PR #2 段只补 foundation 报告/证据已随分支历史纳入、原身份不扩大及 main 未合并的事实。foundation 旧“当前阶段A完成/B仍未通过”状态不覆盖当前状态；它在保留的 Git 父历史及独立报告中可追溯。 |
| docs/log/2026-10-08.md | 保留 research 原文件字节作为完整前缀，再追加 foundation 独有三条原文（去掉重复日期标题，不改三条标题/段落）。附一条明确的历史导入分隔注释，避免把分支间顺序误当严格时间排序。它们是导入既有历史，不是新开发日志；本次合并动作另在实际当天日志顶部记录。 |
| docs/log/README.md | 保留现有日期行、不重复添加2026-10-08；该行摘要联合提及基础修复独立交付及research当日工作，2026-10-09行使用当天最新实际进展。索引更新不修改旧日志内容。 |

foundation 原始三条日志的完整标题：

- 修正README旧CLI MTP推荐，保持基础修复范围
- 基础修复A验收完成，形成可审查合入成果
- 基础修复可合入包开始独立整理

需原样保留其明确限制：旧HTTP复用而非新binary重跑，跨模式/取消失败
和CPU工具首错保留，A可审不等于MTP正式支持，不自动合并或部署。
research 所有旧条目也完整保留，不能为新结论修写历史。

## 自动并入的独有内容

共同祖先之后 foundation 新增17路径，其中16路径在research不存在且
预览树已经逐字保留：`docs/MTP_FOUNDATION_2026-10-08.md` 加
`docs/evidence/mtp-foundation-20261008/` 下全部15文件。第17路径就是
双方新增的2026-10-08日志，需要上述原文并集。

16个非冲突新增文件按 foundation blob 原样接受，不重新压缩、改路径、
整理字段或替换快照。证据包括：旧证据审计；来源/target核验；首次构建
身份差异记录；execution identity；fatbin容器与CPU来源独立核验；方法
审查；fatbin parser 首次失败，以及5份实际复核/构建脚本快照。JSON列有
全路径、字节数、blob和SHA，确保没有漏掉原失败或“精简”原身份限制。
独立报告里的历史分支、当时入口限制和状态也是历史，不改写为M2结论。

## 合并后仅做身份与文档核对

先将 M2 的实现/证据清楚提交；保存该提交的 runtime/test/tool/CMake 和
现有生产/两变体二进制及构建清单摘要。执行真正的两父合并并解决上述
六处。验收合并结果应确认：

1. foundation 与 M2 snapshot 都成为新提交祖先；main 和原dirty工作区
   未变，foundation 原分支/worktree也不需要改动。
2. CMake、include/src/tests/tools/cmake 与 M2 snapshot 无内容差异；
   所有现存已受测二进制/原始证据摘要相同。没有必要重新配置或链接。
3. 16个独有新增文档/证据逐字对应 foundation；两个原日志body都作为
   连续字节块完整保留，三条独有标题各出现一次；无冲突标记/重复日期行。
4. 所有新相对链接可定位；当前STATUS不把历史报告、旧HTTP或旧性能
   当成当前M2实测。当天新增合并日志明确什么改变、为什么及下一步。
5. PR #3 描述更新依赖已纳入同一分支；PR #2 仍可独立审查，不自行关闭
   或合并任何PR。是否合入main留到完整具体结果可审后处理。

该合并不会解决T4数值准入缺口，也不会改变当前严格参考的性能成本。
