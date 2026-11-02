# SRV-05b 开发与未完成HTTP验收原件

本归档包含155份直接复制记录，另有本说明、本地大文件身份、
源码身份和根seal。运行时55cfae11、第一处CLI修正91652746；后续目录/
CPU预检修复的具体文件SHA见source-provenance.json，随本证据一起提交。

两次启动均在模型加载前退出；0生成、0负例、0模型加载。COMMAND_FAILED
不是验收通过，也不是ENV_ABORT。候选c02b9414未交付，默认仍ecd5d21f/
91ce0ed9。原package/execution及correction均保留；封包中的旧驱动源码
以本地source.bundle为准，不以最终修复版本倒写失败历史。

最终CPU在cpu/，冻结时的原CPU在两个package/cpu/。生产启动预检5项
仅证明真实选项解析与SSD目录direct IO，不证明GPU/HTTP。模型权重只读。
第一条CPU tokenizer目录参数误用的stderr未落盘，仅事后记录；其余
已保存输入/输出按原样复制。大ELF/bundle在本地保留，身份见local-identities。

根sealed-files.json覆盖本归档每个其他文件；两个package中的seal还
引用未内嵌的大文件，需连同local-identities指向的本地包验证。完整结论见
[阶段结果](../../SERVE_THINKING_BUDGET_RESULT_2026-11-03.md)。
