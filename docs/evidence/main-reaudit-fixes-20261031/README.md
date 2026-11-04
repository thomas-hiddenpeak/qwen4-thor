# main复审四项修复原始证据

本目录逐字节归档172份证据；[files.json](files.json)记录原路径、
SHA256和字节数。运行时源码/工具锚点55299a0，父版本ef88d97，
原审计父版反例保留在[上一轮证据](../main-reaudit-20261031/README.md)。

- cpu：31 IO/专家合同、29预算、109 Thor host、公共CTest 22/22、
  10监督器和5回退合同，以及四套零警告构建的命令/完整输出。
- measurement：同一共享GPU锁下的2进程/21合同和16HTTP/8模型启动，
  四模式父候选完整响应、资源/生命周期、逻辑读取及监控原始记录。
- deployment：实际4请求槽full/subset各3请求，默认替换/健康/guard
  记录及finalized收据；两head公共8192/私有9230缓存命中。
- plan、decision、source-review、final-audit绑定范围、身份、直接
  修复、原始计时与工程接受；没有五档性能或严格物理SSD/RAM声明。
- delivery-package中的JSON为封条/发布元数据副本。可执行文件、
  真实构建缓存、完整源码/工具、head和Git bundle保留在原.q4t-work
  路径，不把Git中的文本证据当作完整程序回退包。

测试中的故障是预期负向合同，CPU/真实CUDA合同均返回成功；本次
没有非预期失败、ENV_ABORT或追加采样。五项真实CPU监督场景使用
受管组的自有子进程，测试收尾仅对这些身份发信号并完成wait。
原审计反例及前8项修复的历史失败没有删除或改写。

结果与回退入口见[修复交付](../../MAIN_REAUDIT_FIX_RESULT_2026-10-31.md)。
CPU命令见各commands.json；checks.py是本轮检查编排原件，只能在
原目录身份适用时使用，重复验证应改为新的输出目录保留旧证据。
