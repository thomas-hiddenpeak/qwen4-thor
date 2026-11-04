# 评分改动的 Serve 来源清单复核

PR #9 首次 host 检查在清单步骤失败：CMakeLists.txt 与 src/mtp/mtp.cu
两个锚点变化；前面的 host 构建与合同步骤完成。原失败保留在 GitHub
[41853786341](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/41853786341)。
本机定向复现同样失败，审查后只更新两个来源摘要并追加审查范围；
38 输入字段、10 路由、20 白名单、112 锚点保持，三种变异检查均拒绝。
这只证明清单新鲜度，不证明业务语义完整或性能优化通过。

- CMake：仅增加 EXCLUDE_FROM_ALL 的评分 GPU 夹具目标；默认 runner
  链接、编译选项和既有测试入口保持。
- MTP：原参数、槽位和位置验证及 CUDA 失败清理保持；host 已知同槽且
  T>4 的多槽预填充才传评分切片。KV/index 写入仍 pooled，单槽、混合槽
  与 T<=4 保持原分派，既有最小槽容量边界仍生效。
- Server 合同：字段、路由、白名单、模板、输出解析、调度、配额、取消
  实现没有改动。评分改用 BF16 GEMM 的数值与性能须单独验收。
- 协议源码指纹因 MTP 文件变化成为 cc321239，不能自动迁移旧托管包或
  持久任务；未验客户端没有纳入。

为保持全部冻结输入的绝对路径和摘要，测试工作区 source 固定在
2a0a92c5 detached；同一 codex/mtp-prefill-adoption 分支转到相邻 review
工作区，仅承载清单与后续结论。没有增加研究分支或改变测试 ELF/来源。
原工作区十个客户端 WIP 保持；旧研究分支不动。

[复现失败](evidence/mtp-prefill-scoring-20261104/inventory-before.json)、
[审查后检查](evidence/mtp-prefill-scoring-20261104/inventory-after.json)、
[来源与范围](evidence/mtp-prefill-scoring-20261104/inventory-source-review.json)
均为 CPU 静态检查，没有新增 HTTP/GPU 请求。
