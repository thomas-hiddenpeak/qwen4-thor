# rev434 发布与同能力回退

正式默认服务为 rev434，选中主包，期望状态为 STOPPED。版本注册从 145 到 146，容量配置从 36 到 37。两个包均支持 `beta=true`，使用相同的已验收模型和媒体程序。主包另含客户槽位确认冲突的准入修复；回退包保留前一版观察器。rev432 的旧引擎缺少这个选择参数，已验证不能把它登记为新功能的自动回退。

在实际托管端口上，向本次测试自己的主网关注入致命退出。Guardian 保持同一进程，设备探测一次，已注册的回退包接管。两次模型启动、110.70 秒；前后 WebRTC 均建立且完整关闭，普通 Chat 均回答 42，能够回读故障发生前的 Beta 回答和 Chat。注册到正式服务前后，13 个受保护文件、配置切换预算与期望停止状态保持一致。GPU 租约完成，端口及自有进程清理。历史的启动期通用 503 仍缺少能判定原因的记录。[摘要与原始回执身份](delivery.json)

## 当前版本的 24 小时运行入口

从项目根目录运行以下命令。计划仅使用已经固定的 rev434 服务包、来源和独立状态，第 1 小时有检查点。原有 rev432 计划及其三周期短验仍可回读，本次只校验 rev434 的长稳计划。24 小时由操作员另行执行，已运行 0 小时。本计划负载覆盖文本、流式、结构化输出和项目资料工具；音视频、电话及 WebRTC 连续负载另行推进。

```bash
cd .q4t-work/serve-responses-beta-20261123/soak-src
/usr/bin/python3 -I -B tools/serve_soak/native_stage.py check --plan .q4t-work/day-01/plan.json
/usr/bin/python3 -I -B tools/serve_soak/native_stage.py run --plan .q4t-work/day-01/plan.json
```

结束后在同目录用 `/usr/bin/python3 -I -B tools/serve_soak/native_report.py --output .q4t-work/day-01` 生成报告。需要中止时使用 `.q4t-work/day-01/handoff.json` 中绑定私有状态的停止命令。

Live 与旁路观测改善已随 [PR #58](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/58) 合入；稳定 SDK Beta 选择参数及 rev432 交接随 [PR #59](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/59) 合入。每条 PR 的两项最终检查均通过。具名多代理执行仍是待实现的接口能力。
