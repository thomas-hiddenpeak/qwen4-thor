# 发布合同与主机前置门禁

首版受控部署范围：单Thor、文本、greedy、MTP关闭、默认loopback，
正式性能配置max_seq=1，五档上下文见docs/EVALUATION.md。媒体默认
在解码前拒绝；`serve --allow-media`保留实验路径，不在首版验收范围。
这不表示Runner商业发布、真实业务SLO、认证/TLS或全部数值合同已通过。

## 不可误绿的主机检查

公共Linux开发/CI入口（CMake >= 3.25、C++23编译器、Python 3）：

```sh
cmake -S tests/host -B build/public-host -DCMAKE_CXX_COMPILER=g++-14
cmake --build build/public-host --parallel
ctest --test-dir build/public-host --no-tests=error --output-on-failure
python3 tools/verify/test_gate/run.py --cxx g++-14 \
  --output build/public-host/test-gate-new
```

公共入口与Thor共享host测试源码清单，实际运行全部18项JSON/HTTP/
请求合同及16项测试框架反例，无CUDA、ICU、模型文件或私有证据依赖。
反例输出目录每次新建。公共CI不构建runner，不覆盖真实tokenizer、
GPU完成/状态、HTTP推理或五档性能；下面的22项发布检查保持独立且必需。

Thor发布主机入口：

```sh
cmake -S . -B build -DQ4T_BUILD_TESTS=ON
cmake --build build --target q4t_host_tests --parallel
python3 tools/release/check_host_contracts.py \
  --test-binary build/q4t_host_tests \
  --output .q4t-work/host-gate-new
```

此命令使用已跟踪的required_host_tests.txt，按精确名称要求22项JSON、
HTTP、请求合同与真实tokenizer检查。不能传自定义筛选缩减检查项，
缺少ICU/模型/注册项或任一SKIP都不能通过；输出目录必须新建。
二进制/清单摘要、注册列表、原始日志和passed结果写入输出目录。
它是发布的**主机前置条件**，不能代替下面的运行时/数值/性能验收。

q4t_tests与q4t_host_tests共享以下行为：

- 默认严格：失败、异常或SKIP退出1；零匹配、空清单、未知/重复名称
  或非法参数退出2；仅全部所选测试实际PASS退出0。
- `--required-list FILE`先验证所有精确名称，再执行，防止部分跑完
  才发现拼错名称。required模式禁止--allow-skips及模糊筛选。
- `--list`枚举注册项；普通位置参数仍支持开发时的子串筛选。
- `--allow-skips`仅供明确的开发探查，输出仍显示SKIP，不能用于发布。
- 原89处缺设备/fixture分支使用Q4T_SKIP，中止整个测试并正确展开栈；
  不再打印skipped后返回true。测试总汇的绿色不能包含未执行项。

完整q4t_tests包含视觉/MTP等需要额外fixture的检查，可能因缺少fixture
非零退出；这不是模型计算退化，也不能通过恢复“skip算pass”解决。
2026-09-27节点编译完整测试程序并运行精确主机子集和缺GPU反例，未宣称整套
数值测试全绿。需要的GPU数值合同仍按独立数值计划与原始证据验收。

## 运行时发布必须另外回答的问题

1. 缺陷直接回归与关键取消/失败终态是否符合合同，绑定哪个二进制？
2. 固定HTTP质量、模型模板入口与正常/错误SSE是否通过？
3. 五档单流MTP关闭E2E输出与性能是否满足既定接受规则？
4. 部署的模型、配置、二进制与证据身份是否一致，如何回滚？
5. 业务负载、队列/并发、恢复监督、认证与服务目标是否另行验收？

目前提供的是主机门禁及已有运行时验收工具的明确分工，没有一个能
自动证明全部商用条件的“总通过”按钮。阶段提交、主机通过或HTTP200
都不能替代未完成项；当前部署与候选结论以docs/STATUS.md为准。

## text-v1 受控发布包（2026-09-28）

本版正文预算48MiB（声明长度预占，贯穿读取至响应结束），控制正文
4KiB，文本解析/排队/生成合计8个（max_seq=1）。上传绝对30秒，
超时408；正文或文本准入耗尽503；代理生成准入4，超限429。
生成与排队默认/最大20分钟，request_timeout_ms只接受[1,1200000]，
可以缩短。大于该值明确400，不静默夹限。代理头部最多10秒，故
代理头部+正文最坏40秒；正文流式转发，不落盘，拒绝chunked上传。
这些是输入引发分配的界限，不承诺整个共享Thor永不OOM。

```sh
python3 tools/release/release.py package \
  --binary .q4t-work/convergence-20260928/build/q4t \
  --cache .q4t-work/convergence-20260928/build/CMakeCache.txt \
  --output .q4t-work/releases/text-v1
python3 tools/release/release.py activate \
  --root .q4t-work/text-service --package .q4t-work/releases/text-v1 \
  --nginx /absolute/path/to/nginx
python3 tools/release/release.py status --root .q4t-work/text-service
python3 tools/release/release.py stop --root .q4t-work/text-service
python3 tools/release/release.py rollback --root .q4t-work/text-service
```

包保存二进制、真实构建缓存、源码快照、代理/监督配置和SHA256清单；
模型只读，身份记录JSON配置/索引/tokenizer摘要，不冒称重新校验了
全部权重字节。副本不修改模型。激活前校验包与模型配置，启动失败
恢复旧指针并在原先运行时重启旧版。rollback需要已有previous版本。

默认后端18080、数据18081、控制18082，全部127.0.0.1；首次激活可
指定不同非特权端口，后续使用持久化配置。无TLS/认证/公网监听，
由可信本机客户端或外部认证网关访问；同主机恶意租户不在隔离承诺内。

监督器重启退出的后端/代理，清除死亡代理主进程的遗留worker；GPU
不健康要求重启，30秒不能退出则强杀；活动请求1260秒无生成token计数进展触发
执行看门狗。5次/300秒重启后明确停止，不无限重启。进程日志每份
4MiB加一个轮转副本，代理请求/响应不写临时磁盘；历史发布包和
测试证据另由操作者保留/清理，不属于在线请求产生的临时数据。

提供q4t-text.service.in供外部user systemd监督本监督器，KillMode
使用control-group；未自动安装或启用。使用该单元时，升级/回滚前
先停单元，更新指针后再启动，不能同时运行两套监督器。独立模式
监督器自身被SIGKILL后的恢复需要外部服务管理者；不承诺主机故障
或GPU硬挂可在进程内恢复。

统一验收入口（先完成成组修改与必要构建）：

```sh
python3 tools/release/qualify.py \
  --build .q4t-work/convergence-20260928/build \
  --output .q4t-work/convergence-20260928/qualification \
  --previous-binary /absolute/path/to/previous-package/q4t \
  --previous-cache /absolute/path/to/previous-package/CMakeCache.txt \
  --nginx /absolute/path/to/nginx
```

入口失败即停并保留原始记录，没有自动反复重测。冻结清单与30分钟
试运行预算见docs/CONVERGENCE_2026-09-28.md；它验证明确私有部署范围，
不能被描述为公网多租户商业SLA证明。数值证据复用和性能代价另列，
不以单一退出码宣称整模型数学正确。

源码构建与已发布二进制分开追踪：默认build/q4t部署后有
q4t.release.json，记录已验收包和真实构建缓存摘要。E2E工具优先
验证该身份；后续自行重建若改变二进制，会明确拒绝旧部署元数据，
须作为新候选验收，不能把旧缓存/旧通过结论自动套用到新文件。

统一入口自带压缩冻结夹具及逐文件摘要，解包后与本次实际使用的
16个输入/参考文件字节相同，不依赖隐藏历史测试目录。旧版本二进制
及其真实构建缓存通过参数明确提供，用于实际升级/回滚；不能用一个
不同身份的缓存冒充旧版本来源。首轮原始编排脚本保留在证据中，
新增夹具恢复入口仅做离线字节等价验证，没有为路径整理重复推理。
