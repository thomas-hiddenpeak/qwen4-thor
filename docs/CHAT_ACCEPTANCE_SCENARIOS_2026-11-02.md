# 聊天接入闭环与故障场景开发

基线d0f06b1，继续用户的只开发、不测试要求。当前工作仅准备脚本、
合同和静态编译记录；不执行入口、信号探测、CPU/GPU测试或HTTP。
默认程序和vLLM业务不动，不扩大tool_choice或schema能力。

## 冻结场景与预算

| 场景 | 实现路径与预期 |
|---|---|
| 普通decode JSON完整往返 | 正式ChatClient和demo，lookup一次、回填后回答831907；恢复判定COMPLETED |
| 普通decode SSE完整往返 | 同上，两轮均真实流式传输；与JSON消息/结束/usage对照 |
| MTP SSE完整往返 | 同上；第一轮再与同请求的JSON响应对照 |
| 父进程SIGTERM / SIGINT | 固定本地计划和无模型guard夹具，真实execute及子进程；重复信号不打断清理，结果不得PASS |
| 提交记录已落盘后SIGKILL | REQUEST_OUTCOME_UNKNOWN，不自动重发 |
| 部分wire已落盘后SIGKILL | REQUEST_OUTCOME_UNKNOWN，不解析半段或执行工具 |
| 工具started已落盘后SIGKILL | TOOL_OUTCOME_UNKNOWN，不把未知结果当未执行 |
| 工具result已落盘、history未写时SIGKILL | 从已知结果重建完整历史，READY_TO_REQUEST |
| 工具history已落盘后SIGKILL | 保留相同完整历史，READY_TO_REQUEST |
| 最终response已落盘、history未写时SIGKILL | FINAL_RESPONSE_SAVED，不重放 |

三个真实demo各最多2次生成、1次本地只读lookup，每次生成最多256
输出tokens。
替换既有HTTP计划中6次手工工具/续接请求；其余对照继续保留。
总计仍23次生成（上限24）、10负例、3次模型启动，HTTP30分钟。
能力GET由3增加到6（每个demo查询一次），不产生模型生成。

无模型故障阶段固定8个场景、最多12个受控夹具进程启动（不含既有
锁验证的短命git辅助进程，进程快照另留存）、180秒执行预算，
另给30秒退出余量；失败即停，不重试。接入同一验收worker，由阶段
最外层同一GPU锁覆盖；不得再获取嵌套GPU锁或操作其他服务。
总体仍40分钟，guard原有启动/退出余量保持。

## 覆盖边界

故障夹具使用真实进程、信号、demo文件锁与fsync/原子发布，但请求
响应为固定本地输入，网络禁止；不把这部分计为真实模型HTTP质量。
父进程中断使用生产execute清理路径，无模型guard夹具复用实际
stop_owned清理；不把夹具视为GPU监控/竞争检测的独立验证。
真实GPU锁及监控仍由完整验收最外层提供。

子进程须完成边界握手后才注入信号，不能用固定睡眠猜执行位置；
只向持有pidfd/自有Popen句柄的本轮进程发送信号。保存原始日志、
进程退出和恢复判定，检查恢复器没有改写日志；强制清理补救不能
当作预期清理通过。SIGKILL边界仅证明进程中断，不证明断电或磁盘
故障恢复。未知工具副作用仍须外部对账，不提供自动重放。

## 开发状态

脚本和编排已开发完成，**全部运行验证未执行**。仅7个Python文件
语法编译；客户端22/恢复13/编排4项离线合同定义仍未运行。本轮
扩展既有客户端合同的输出token上限传递、编排合同的故障步骤及
ENV_ABORT传播检查，没有把真实进程场景混入公共host CTest。

- `verify.py`直接运行正式demo，预算包装层委托ChatClient的真实
  HTTP/SSE传输，仅禁用loopback环境代理；每轮保留请求、原始wire、
  消息、工具记录及传输摘要，完成后用恢复器重读并要求COMPLETED。
- `verify_failures.py`负责握手、pidfd信号、只读恢复核对和自有
  进程回收；`fault_fixture.py`仅在真实阶段锁及祖先核对后启动。
  父进程夹具只替换计划校验和guard启动命令，保留生产execute的
  等待/清理/finally路径；没有嵌套真实GPU guard。
- `acceptance.py`增加faults步骤，位于模板核对之后、HTTP之前。
  超时、缺少故障阶段收尾记录或清理失败记ENV_ABORT，并传播到
  worker和总结果；普通合同失败保留FAIL，任何失败均停止后续步骤。
- demo增加`--max-tokens`（默认512，允许1～512），本计划固定256；
  不改变引擎接口。未来freeze会记录新工具源码和新增预算字段，
  旧计划不可复用。冻结/执行命令沿用
  [统一入口说明](CHAT_INTEGRATION_PREP_2026-11-02.md#统一有界验收入口当前不要执行)。

164项运行时源码及候选830694ce、host 9e66abd4、模板f1aff912、
默认0c5ab1de/sidecar e6fdd13b身份保持；没有重编、部署或操作服务。
[开发证据](evidence/chat-acceptance-scenarios-20261102/README.md)
仅记录语法编译和身份，不能作为场景通过。下一步是在业务允许的
窗口重新freeze，再由同一独占锁完成有界验收；当前不自动等待或补测。
