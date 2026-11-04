# 聊天接入闭环与故障脚本开发证据

状态：**DEVELOPMENT_SYNTAX_ONLY_TESTS_DEFERRED**。基线d0f06b1。
本目录不是测试结果，不能作为验收或默认程序发布依据。

准备3套正式客户端/工具demo闭环，以及SIGTERM/SIGINT两项真实父
进程中断和6项日志落盘边界SIGKILL场景；**全部未执行**。故障夹具
使用固定响应，不能代替真实模型质量；本轮连夹具也没有启动。

- `syntax.log`：7个Python文件仅内置compile，无项目模块导入、
  无执行或字节码缓存；针对后续源改动补编的记录追加在同一日志。
- `scenario-definitions.json`：AST静态读取的预算与8个场景定义。
  不是实际发送信号、进程启动或成功恢复的证据。
- `development.json`：测试、HTTP、真实信号、夹具/模型启动均为0；
  Python合同仍22/13/4。23生成/10负例/3启动、40分钟预算保持，
  能力GET为6次；新增故障180秒、12个受控夹具进程的未来计划。
- `before.json` / `tool-source.json`：父提交与最终工具源码身份。
  两个新文件的修改前身份为null，表示尚不存在。
- `runtime-source.json`：164项运行时源码不变；八项既有产物SHA256
  均未变化。包括候选830694ce、host 9e66abd4、模板f1aff912、默认
  0c5ab1de和sidecar e6fdd13b；没有重编引擎或替换发布文件。

原始记录位于`.q4t-work/chat-acceptance-scenarios-20261102/`，候选
与原构建证据仍保留在前阶段目录。后续须以当前源码重新freeze，
由同一真实GPU租约执行完整阶段；当前没有运行任何入口、探测、
测试或示例，没有操作vLLM及其他服务。提交使用`[skip ci]`。
