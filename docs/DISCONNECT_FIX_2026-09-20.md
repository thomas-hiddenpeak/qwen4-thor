# 断连修复与 E2E 验收记录

日期：2026-09-20。结论：断连专项通过；完整精度与性能无回退验收仍未通过。

后续定位见 [输出复现报告](REPRODUCIBILITY_2026-09-20.md)：4K 差异已定位
到稀疏索引展开顺序，固定顺序后六次跨进程重放一致。以下保留断连阶段证据。

## 根因与改动

修复前，真实 evalscope HTTP 请求上传后经代理注入 TCP reset，服务进程退出
-13，确认 SIGPIPE。src/server/chat_server.cpp 的 WriteAll 使用 write，
对已断开的 socket 写入可能终止整个进程。

改为 send(MSG_NOSIGNAL)，保留短写和 EINTR 处理。流式响应首次写失败后
停止写入，中止只计一次，不再同时计成功；响应头写失败直接释放 sequence，
decode 中途失败沿现有调度注销和资源清理路径退出。未改模型计算和 kernel。

这不是 GPU prefill 抢占取消：已提交的计算仍可能完成后才检测写失败。
专项覆盖 MTP 关闭、单槽位、流式请求；不宣称慢客户端、MTP 或所有断连
时序均已验证。非流式成功计数没有在本轮重新设计。

## 验证顺序与结果

必要构建后首项测试是 tools/evalscope/disconnect_e2e.py 驱动的真实 HTTP
evalscope E2E，没有前置单测、bench 或 profile。构建日志无警告。

上传后、首内容块后、第八内容块后断连，各两轮。每轮随后发正常恢复请求：

- 六次恢复均成功，实际输入 1024、输出 64 token，输出文本全部一致。
- 中止与成功计数分别从 1 累积到 6，没有重复计成功。
- 每轮结束服务 FD 数均为 49，单槽位可复用；服务存活，最终正常退出 0。

复跑命令（fixture 必须是实际 1024 token 的固定请求；使用新的结果目录）：

```bash
python3 tools/evalscope/disconnect_e2e.py \
  --fixture .q4t-work/e2e/20260920-baseline/context-1024/requests.jsonl \
  --out .q4t-work/e2e/disconnect-repeat
```

## 五档正常请求

MTP 关闭，每档三条固定输入，各输出 256 token；15/15 请求成功，长度全部
达标。TTFT 包含 HTTP、排队等开销，不是纯 prefill。decode 使用
sum(output_tokens−1)/sum(latency−TTFT)。

| 输入 token | 旧 TTFT / 新 TTFT（秒） | 旧 / 新 decode（tok/s） | 输出与旧记录一致 |
|---:|---:|---:|---:|
| 1024 | 0.878 / 0.869 | 15.51 / 15.59 | 3/3 |
| 4096 | 3.238 / 3.232 | 14.19 / 14.23 | 0/3 |
| 8192 | 6.519 / 6.513 | 14.85 / 14.80 | 0/3 |
| 45056 | 39.741 / 39.773 | 14.45 / 14.52 | 0/3 |
| 204800 | 234.937 / 234.737 | 13.98 / 13.85 | 0/3 |

请求 JSONL 和实际发送内容相同。性能变化约 -0.91% 至 +0.53%，但每档仅
三条不同输入，尚无重复噪声带；生成路径也不同。200K 运行期间进行过短暂
的隔离旧版编译，进一步限制性能归因。不能据此宣称性能无回退。

## 旧二进制对照：现有基线不可完全复现

在隔离目录恢复断连修复前的二进制，SHA-256 与原基线完全相同：
16a67b3615db8e2f7635e18f1da82aa49b8bf1c42e01ddc7dd890d0e8496a687。
没有覆盖工作区代码或候选二进制。

同配置、同请求顺序重放 1K 和 4K：1K 三条与原基线及候选版全部一致；
4K 三条与原基线及候选版均不一致。因此旧版自身存在跨运行输出复现问题。
这不足以排除补丁的额外影响，也不能直接解释成量化噪声或判断为精度下降。

当前只接受断连专项结论，不把完整回归标为通过。下一步应以旧版同输入
同进程重复和跨重启 E2E 缩小复现条件，再同样比较候选版；在此之前不推进
kernel 优化，不用放宽精度门槛绕过问题。

## 本机证据

证据位于 .q4t-work/e2e/（未纳入版本管理）：

- disconnect-before/result.json：修复前退出 -13。
- disconnect-after/result.json：六轮断连、恢复、计数、输出摘要及 FD 数。
- disconnect-build/build.log：必要构建记录。
- 20260920-disconnect-regression/：五档 evalscope 请求、数据库与日志；
  comparison.json 保存逐请求计时和输出比较，exit.json 记录正常结束。
- disconnect-control-build/：恢复的旧二进制、源码、构建命令和日志。
- 20260920-control-replay/：旧版重放；comparison.json 保存三方输出比较。
  实际服务二进制以 server-binary.sha256 和 server-command.json 为准；
  通用脚本生成的 binary.sha256 指向工作区候选版，metadata-note.txt 已说明。

所有本轮测试服务均已停止。分词计数差异、正常 messages 格式、长上下文
召回与质量评测仍是独立待办，本轮裸 prompt 性能负载不能替代它们。
