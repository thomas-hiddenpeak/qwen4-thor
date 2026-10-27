# 固定k3 suffix与长短agent并发主线合入

2026-10-27，按用户“开始并入主线”的明确授权，已将已交付阶段快进合入
并推送远端main。原main为 `a75643c9fed8c965ca279b69a12d72bc88e55107`，
合入实现及交付锚点为 `36266bb422c8e6fc61358fa82e0068684fbbe617`。
58个提交完整保留，没有squash、rebase或合并冲突；500个差异文件包含
固定k3实现、数字准入/按需分类、四槽容量和分块prefill、监控修复、测试，
以及本阶段原成功/失败研究证据。本次没有新增推理改动。

## 已交付身份与证据复用

默认程序仍为 `f2a6de360e05676c5fee2f86fbd314c9a9ff3ea0378fa104e0185cef6b4ec164`，
发布元数据仍为 `26325699af40325e384c4aae4d615679f2d7e2aa69f9eecbc5743e68db6032cf`。
160项运行时文件同时核对Git内容、工作区、实际编译清单及发布包源码，
全部一致；cache、接受快照和审计引用SHA有效。202文件程序包与118文件
交付归档封条逐项通过。发布来源保留c1ad148基点加工作区输入，不改写成
主线合入提交或本次文档提交。

复用320条验收HTTP和3条部署健康请求；本次无本机构建、模型加载、GPU
请求或性能重采样。main触发的[公共host合同检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/40488752080)
通过，仅证明实际执行的无CUDA/无模型CPU检查，不冒充Thor端数值或性能验证。

完整head仍默认，suffix须显式启用，65536行BF16子集仍独立选择。
交付范围为单流及 `208896,65536,65536,65536` 四槽、4096 prefill配置；
32突发是短请求证据，不是32长请求SLA。原子集3%decode收益目标FAIL、
保护负点、统计非劣未建立和已披露自由输出差异保持。用户本项工程接受
不因主线合入扩大或追改为优化达标。用法与回退见
[交付记录](MTP_SUFFIX_AGENT_DELIVERED_2026-10-27.md)。

## 分支与路径收尾

清理范围事前限定为本地及远端 `codex/suffix-k3` 两个引用，旧tip为
`36266bb422c8e6fc61358fa82e0068684fbbe617`。删除前重新核对远端main与
分支tip相同、独有提交0、打开PR为0、当前工作区干净且已切换main，
没有其它worktree占用该分支。远端删除使用精确旧tip的force-with-lease，
本地使用普通 `git branch -d`；均成功，当前有效本地分支为main。

所有历史研究提交由main完整保留，另有
`.q4t-work/suffix-main-merge-20261027/premerge.bundle`作为额外Git备份，
它依赖原main a75643c历史，bundle verify通过。没有删除或搬移任何
worktree、模型、回退包、子集产物或原始证据路径；绝对路径依赖保持。
也没有创建或遗留PR。新授权覆盖本次合入及已满足条件的分支收尾，
历史交付文档中的“不自动合入main”仅描述当时状态。

预检及删除条件见[合入预检](evidence/suffix-main-merge-20261027/preflight.json)、
[清理预检](evidence/suffix-main-merge-20261027/cleanup-preflight.json)和
[实际清理](evidence/suffix-main-merge-20261027/cleanup.json)。
合入后核验见[主线身份记录](evidence/suffix-main-merge-20261027/postmerge.json)。
本次收尾只增加文档和证据；后续工作从main开始，新需求另行冻结范围。
