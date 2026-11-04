# 聊天接入准备开发证据

状态：**DEVELOPMENT_BUILD_ONLY_TESTS_DEFERRED**。编译不是验收通过。

- `development.json`：基线3756269、候选830694ce、host 9e66abd4、
  未变默认/旧候选、源码和未执行范围。
- `build.log` / `final-build.log`：q4t_server与Thor host实际编译，
  生成源码身份交叉核对补充后完成最终构建，零警告。
- `candidate-link-command.json` / `candidate-link.log`：只将CMake链接
  输出/依赖文件改到阶段目录；没有链接覆盖默认程序。
- `syntax.log`：8个Python文件仅compile，没有导入/执行。
- `runtime-source.json` / `tool-source.json`：本阶段运行时及工具身份。
- `protocol-identity.json` / `protocol-source.json`：本候选编译时
  协议/模板摘要及选定源码清单；不是运行时能力响应或发布凭证。

原始目录`.q4t-work/chat-integration-prep-20261102/`保留候选、host、
同身份模板工具副本、CMakeCache和构建记录。新freeze/execute入口和
恢复检查器均没有运行，也没有创建已验收发布元数据。所有测试、
模型请求、模型启动与部署为0；未操作vLLM；提交使用`[skip ci]`。
