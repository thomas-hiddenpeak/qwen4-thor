# rev438 原生维护工具与启动期媒体隔离

当前默认包为 rev438，回退包为先前实际验收的 rev436 主包。两者的完整引擎清单、网关、Guardian、媒体工作程序、主轻配置及模型权重相同。包内四个维护与发布工具文件有变更。正式注册为 148，容量配置维持修订 38，服务期望 STOPPED。发布、注册及安装后的原生 CLI 检查启动模型 0 次。[逐项回执](checks.json)

正式维护命令已能查询三类记录池。为本次发布预先归档两条可退休版本注册及两个旧发布代目录，核对原始内容摘要和 inode。保留前序的不变业务证据，维护修复的 33 项 CPU 检查与跨根实际归档见 [rev436 记录](../serve-responses-agents-delivery-20261123/README.md)。

在缺少可选媒体运行库的本机启动私有服务。第一笔公开业务直接通过官方 SDK 创建 WebRTC，使用预先生成的有效 OPUS/数据通道 SDP。两个准入决定都允许，第一个 HTTP 请求在 74 毫秒内返回媒体专属 503，随后 Chat 答 42，读回存储成功，Guardian、网关和模型保持原进程。一次启动与清理合计 44.48 秒。先前一次运行在测试对端初始化 GStreamer 时，租约检测到 nvargus-daemon 持有计算上下文，已按 ENV_ABORT 保留，私有进程和端口已清理。原始通用 503 的原因仍缺少同期观测；本次冷启动记录提供了可核对的对照。[冷启动及租约摘要](checks.json)

## 操作员长稳入口

已固定 rev438 的发布包、696 个来源文件和独立状态。第 1 小时设检查点，24 小时实跑由操作员安排，目前 0 小时。当前周期覆盖文本、流式、结构化输出和资料工具。

```bash
cd .q4t-work/serve-responses-agents-20261123/maintenance-soak-src
/usr/bin/python3 -I -B tools/serve_soak/native_stage.py check --plan .q4t-work/day-01/plan.json
/usr/bin/python3 -I -B tools/serve_soak/native_stage.py run --plan .q4t-work/day-01/plan.json
```

用 `/usr/bin/python3 -I -B tools/serve_soak/native_report.py --output .q4t-work/day-01` 生成报告。计划旁的 handoff.json 给出只停止本次运行的命令。

托管子代理及主轻资格已随 [PR #62](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/62) 合入 main，两个 host-contracts 均通过。
