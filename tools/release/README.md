# 发布合同与主机前置门禁

首版受控部署范围：单Thor、文本、greedy、MTP关闭、默认loopback，
正式性能配置max_seq=1，五档上下文见docs/EVALUATION.md。媒体默认
在解码前拒绝；`serve --allow-media`保留实验路径，不在首版验收范围。
这不表示Runner商业发布、真实业务SLO、认证/TLS或全部数值合同已通过。

## 不可误绿的主机检查

```sh
cmake -S . -B build -DQ4T_BUILD_TESTS=ON
cmake --build build --target q4t_host_tests --parallel
python3 tools/release/check_host_contracts.py \
  --test-binary build/q4t_host_tests \
  --output .q4t-work/host-gate-new
```

此命令使用已跟踪的required_host_tests.txt，按精确名称要求21项JSON、
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
本节点只编译完整测试程序并运行精确主机子集和缺GPU反例，未宣称整套
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
