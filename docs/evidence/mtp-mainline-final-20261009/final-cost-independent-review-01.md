# 最终五档成本矩阵独立审查 01

日期：2026-10-09。审查者：`/root/terminal_implementation`。
结论：未发现阻止执行的实际问题，可在冻结身份后运行既定30请求。
这是只读源码/输入审查，没有运行候选、构建、测试、HTTP或模型。
审查时尚无 `final-cost-protocol-01.json`，以下审查生成该协议的脚本；
实际执行仍需 `freeze` 生成文件，并由 `run` 再校验绑定摘要。

## 核对结论

- `HERE.parents[1]` 正确定位当前 worktree；候选固定为
  `build/mtp-mainline-final-20261009/q4t`，输入固定既有
  `.q4t-work/evidence/performance-off-04`。五份输入各3行完全相同，
  JSON仅有prompt，不含会覆盖CLI生成参数的字段；不是重新生成或择优选题。
- 普通模式先跑，工具明确传 `--no-mtp`；sequential随后明确传
  `--mtp --mtp-verifier sequential`。每模式按1024/4096/8192/45056/204800
  递增，每档3请求，共30。服务固定S1、208896/8192、输出256；请求固定
  greedy、seed20260920、stream、并发1。新输出目录禁止覆盖既有运行。
- 工具逐档保存原请求、evalscope命令/日志/DB、响应文字/ID及计数；任何
  请求失败、实际长度不符、提前stop、三次不确定、路径回退或启动容量
  变化均失败。sequential对本轮plain逐档比较同输入摘要与三次全文摘要；
  两模式都独立要求实际输入长度、输出256和finish=length，因此配对
  不依赖旧版本输出。不是token ID逐个独立比较。
- 外层没有warmup、重试或额外生成探针；受委托工具每档带number3、
  warmup0、no-test-connection。已只读核对当前安装evalscope的
  `benchmark.py`、`core/metrics_consumer.py`、`core/http_client.py`和
  `plugin/datasets/line_by_line.py`：连接探针被直接跳过，生成总数为
  number+warmup，JSON输入不改prompt，真实POST执行没有生成重试循环。
- 每模式整体2400秒界涵盖其服务和客户端。独立进程组包含runner、server
  与client；超时/错误保留原输出并停止后续模式，必要时对所拥有组发
  SIGTERM/SIGKILL。正常出口要求runner退出0且组无后代残留；强制清理
  不可判通过。外层逐模式保存execution记录及总summary。
- freeze绑定候选、CMakeCache、脚本、tools/evalscope顶层Python源码、
  五份输入和checkpoint metadata；run运行前复核SHA。内部runner另外
  保存binary SHA、commit、worktree diff、实际构建cache、服务器命令、
  evalscope版本及真实请求参数。全部Q4T_*和LD_PRELOAD由外层移除。
  checkpoint权重payload没有重新认证；当前安装evalscope并未被该脚本
  逐源码冻结，版本及实际CLI/DB另存，不能表述成整个依赖环境逐字绑定。

## 输入 prompt SHA-256

| 实际目标输入长度 | 原文件行数 | prompt SHA-256 |
|---|---|---|
| 1024 | 3 | `a72fade2611451e1f33ca088f0554474ec9d9032efccf04cc5581a2c25be2d98` |
| 4096 | 3 | `0cb9f47cdf9ea33d5fe3f4267bcfd347494b3883770c8b293bb5aacf9bb2602d` |
| 8192 | 3 | `e4c57e9fca8d72d694b167a2f56f8527a51e5ab84830983e47ed434d0708e39a` |
| 45056 | 3 | `82da2659b9ef07afac3ef134b735ff8d2ce9c6a37f4565a777f9d76a05d7f478` |
| 204800 | 3 | `959679d2f533ca72d15a5ed3f3a2e2361f63dbf3a88384ab8ec2d750d5e34bf5` |

## 剩余主线门禁

本矩阵接受“当前生产候选的普通/sequential HTTP合同与实际成本”，
没有非回退或提速阈值，脚本明确 `performance_nonregression_claim=false`。
完成30请求不等于性能持平、更快、整模型质量通过或完整Goal完成。
应保留客户端计数来源限制：evalscope DB不保存usage-only原SSE，不能
由本矩阵逐条独立证明计数未触发tokenizer回退；TTFT/decode为客户端估算。

主线readiness文档仍要求独立代码审查、适用测试、明确基础修复依赖和
具体可合并交付。第二组位置/完成/terminal的真实直接与HTTP证据须独立
闭合，不能拿五档成本代替。T4未解决的独立数值/状态资格仍保留实验，
不以默认入口改为sequential或PR状态变化宣称T4获得准入。main合入状态
与原dirty工作区保护也必须另由当前git/PR证据核实。

## 受审文件身份

| 文件 | SHA-256 |
|---|---|
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/.q4t-work/mtp-mainline-20261009/final_cost_matrix.py` | `11dedd011cbffabde87a1ee2e4727b3c3fb9ac7120805fc6ede5903842eb6f28` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/docs/MTP_MAINLINE_READINESS_2026-10-09.md` | `1133d738d3265ca7d784fb8e6748872ca75c707b0e10240e598e3f62cd46fbc3` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/docs/MTP_COMPLETION_FIX_2026-10-09.md` | `121e087b8a1ef071a11064f16c005af230af9881054f9fbfbb0d7a7b84e659d9` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/tools/evalscope/run_acceptance.py` | `20e7972366f2032ecd4f985ea969f5fa4a07d1b78216c969a832e77c13cf175d` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/tools/evalscope/acceptance_mode.py` | `4e99e92616880d5017cc1d04f968c505b7e7bc9e6b2d06f417b6dd9d2f1bf44a` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/tools/evalscope/mtp_mode.py` | `eb85f8b55b693ba4e74e9912beacb285d1162c4a239a9dd1cff4d4548abb049c` |
