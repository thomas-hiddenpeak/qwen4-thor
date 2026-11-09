# A07.4 可回退默认交付

2026-11-09：8 条实际封装/检查/发布命令全部通过。默认引擎
`0c577f54`、元数据 `11eeb4df`、原生包 `c59afb3d`，publication
revision16，STOPPED；核心及客户端 Key/限额/用量账本字节保持。
旧 A07.3 原包 `dca3b450` 与发布代完整保留，安装 0 模型/Chat/GPU。

真实压缩业务与原生入口证据分别见相邻 business/ingress 目录。
新 Ingress `95877fbe`，Guardian/probe 及控制源码保持；config、application
和 OpenAPI 随包更新。复用已经通过的发布事务和旧代回退证据。

首准备在创建 release/修改默认前因 metadata symlink 被普通文件检查
拒绝；改为对已管理别名 resolve 后读封装文件。失败脚本与原件保留，
该错误没有改变候选或模型，不追加 GPU。实际发布 8 条命令首轮全过。

本项完成后连续推进 A07.5 版本化 Prompt 及全部总计划。尚未合 main；
不声称整模型质量、性能或用户负责的长稳已经验收。
