# 聊天业务实机验收证据

状态：**PASS_BOUNDED_CHAT_INTEGRATION**，候选830694ce。完整说明见
[业务结果、预算与GPU交接](../../CHAT_BUSINESS_ACCEPTANCE_2026-11-02.md)。

- `initial/handoff-*.json`：用户授权释放GPU后的交接计划、正常停止
  和原监督器自有清理回执。私有容器配置仅留工作目录，不在本归档。
- `initial/faults/`：8个真实故障场景、12个受控夹具进程，全部通过；
  原始握手、pidfd信号、退出、日志摘要和恢复判定完整保存。
- `initial/http/`、`initial/http.log`：首次基线成功启动，但中文请求
  在POST发送前编码失败；服务退出清理正常，首次失败没有被覆盖。
- `initial/source-before-fix/`与`source-after-fix/`：UTF-8驱动修复及
  新loopback回归合同的前后源码；引擎二进制没有变化。
- `corrected-plan.json`：修复后全部源码/产物/模型配置的冻结身份。
- `utf8-correction/correction-budget.json`：事前修复预算，累计24次
  生成尝试、4次模型启动、7次能力GET，原40分钟总截止不重置。
- `utf8-correction/orchestration.log`：5项编排/传输通过。
- `utf8-correction/http/`：23生成、10负例、3启动、6能力GET及3套
  正式demo往返一次通过；每个请求、响应、日志与退出记录保留。
- 两阶段`lease/`：真实共享锁、NVML监控、退出及自有清理；首阶段
  因驱动异常COMMAND_FAILED，修复阶段COMPLETED，无运行期ENV_ABORT。
- `utf8-correction/result.json`：组合结论、复用CPU/真实故障、累计
  数量、三套工具最终831907、4次模型资源归零及未部署状态。
- `utf8-correction/final-release.json`与`final-nvml.json`：最终计算
  进程为空、共享锁可获取、无恢复标记，保持GPU释放。
- `sealed-files.json`：313份原始文件SHA256，不含本README和封条自身。

既有97 host、22客户端、13恢复、21模板复用
[此前CPU证据](../chat-business-acceptance-20261102/README.md)；编排/传输
修复后5项重验。158项CPU覆盖、8个真实故障以及本轮HTTP均通过，
不能扩大为断电恢复、整模型质量、性能优化、常驻服务或部署验收。

原始工作目录为`.q4t-work/chat-business-runtime-20261102/`及
`.q4t-work/chat-business-runtime-utf8-20261102/`；默认0c5ab1de/
e6fdd13b保持，原vLLM与测试服务均已停止，不自动重建占用GPU。
