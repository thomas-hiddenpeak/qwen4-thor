# 当前描述与实现核对（2026-10-17）

基线7356f23。范围为README、GitHub项目简介、CLI展示文字、AGENTS、
文档总索引、ARCHITECTURE/MODEL及PHASES入口；按当前代码校正，日期报告
和日志保留历史含义，不将它们逐篇改写成当前状态。未运行模型或性能采样。

| 不一致/易误读描述 | 代码依据与修正 |
|---|---|
| GitHub简介C++17 | 根CMake的host/device标准均为23；远端简介已改C++23 |
| README端口8080、方括号伪命令 | ServerOptions默认8000；改为可执行示例与单独默认值表 |
| CLI称MTP默认关、version为skeleton | ServerOptions.no_mtp=false；只修PrintUsage/PrintVersion文字 |
| generate注释声称--no-mtp回退 | RunGenerate未解析该参数；修注释并明确README限制，不新增功能 |
| “当前工作分支”T>1，旧归并中表述 | 现已入main，sequential只在历史保留；指向当前STATUS |
| PLE整表节省/重叠被当成固定效果 | GatherRows中host等待io_uring完成，再在调用者stream上传/转换；不保证延迟全被掩盖 |
| PLE独立ConvertStream、staging少算16倍 | staging=capacity_tokens×16×160，另有同尺寸GPU FP8 scratch；无专属转换stream对象 |
| 旧84GB/14.2GB/96GB当通用内存账 | 改为配置相关的预算说明，不将旧单次峰值或262144配置上限当当前容量保证 |
| NVFP4被理解成全模型精度 | 专家NVFP4 W4A4与其他BF16/FP32路径分开描述 |
| Paged KV/PD-ready被当成完整迁移能力 | 区分页表实现与未交付的多设备PD/共享页池等目标 |
| MODEL上游forward被称最高实现事实 | 上游仅作语义参考，本项目执行以src为准；上游prefetch/history段明确标注 |
| docs索引旧“当前/进行中”被当待办 | 更新现行入口，明确日期专题冻结历史；PHASES旧入口标为历史 |
| 构建步骤遗漏完整推理依赖 | 补pkg-config/liburing-dev/libicu-dev；CMake缺失时禁用相关组件 |
| README直接ctest与GPU互斥约定冲突 | 示例经gpu_exclusive驱动；本轮未执行该测试命令 |

## 主要源码入口

- [CLI及展示文字](../src/main.cpp)、[serve默认值](../include/q4t/server/server_options.h)、
  [参数解析](../src/server/server_options.cpp)、[HTTP路由](../src/server/chat_http.cpp)。
- [PLE缓冲与上传所有权](../src/ple/ple_embedding.cpp)、
  [页池/reader](../include/q4t/ple/page_reader.h)、[主模型PLE调用](../src/model/model.cu)。
- [构建依赖](../CMakeLists.txt)、[启动预算接入](../src/server/chat_server.cpp)、
  [内存预算](../src/runtime/memory_budget.cpp)。

仍存在的实现限制如generate缺少--no-mtp不是本轮功能修复；服务的
capabilities.Experimental仍是MTP/媒体/并发组合的粗粒度标记，不能代替
STATUS中对特定单流文本配置的正式验收。这些不一致不通过修改数值行为掩盖。

## 验证与身份

只改两处CLI字符串与一个注释，其余为描述。q4t目标构建完成、零警告，
版本与无参数帮助输出已检查；没有运行probe、加载模型或重跑HTTP/数值。
构建日志包含qsa_decode对象重编译，但该源码未修改；不据源逻辑相同
声称新binary与旧binary逐位相同或重新通过性能矩阵。

[身份记录](evidence/description-audit-20261017/identity.json)区分新binary
8685d8bf…与原已接受be74946d…；旧binary保存在本地备份及原基线目录。
本轮不是新的模型性能接受，当前证据复用按源码/依赖及实际覆盖范围
说明，不将新hash自动绑定旧HTTP结果。GitHub简介已读回核对。
