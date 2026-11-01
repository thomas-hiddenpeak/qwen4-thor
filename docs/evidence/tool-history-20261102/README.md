# 工具历史关联开发证据

状态：**DEVELOPMENT_BUILD_ONLY_TESTS_DEFERRED**。编译完成不是验收通过。

- `development.json`：基线0fa7a96、候选b4f04236、未变默认0c5ab1de/
  e6fdd13b、host与模板工具身份、9项新增合同定义及未执行状态。
- `build.log` / `final-build.log`：根工程q4t_server与Thor host编译，
  未运行目标程序。媒体顺序边界补正后完成最终构建，零警告。
- `candidate-link-command.json` / `candidate-link.log`：使用CMake链接
  参数，仅候选输出/依赖文件改到阶段目录，没有替换默认q4t。
- `template-checker-build.log`：独立模板工具带-Werror编译，未执行。
- `runtime-source.json`：候选运行时源码摘要；不代替运行时验证。
- `driver-syntax.log`：HTTP驱动只作compile，未导入或执行。

阶段原始文件、CMakeCache及可执行文件保留在
`.q4t-work/tool-history-20261102/`。旧回退包与前两项开发证据保留。
所有CPU/GPU/HTTP测试、模型加载与部署为0，未操作vLLM；提交使用
`[skip ci]`。当前驱动待执行23生成/10负例/3启动，上限仍24/30分钟。
