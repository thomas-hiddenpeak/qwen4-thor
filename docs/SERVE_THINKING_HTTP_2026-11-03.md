# SRV-05b 未执行HTTP的后续验收阶段

本阶段开始前，上一轮已以HTTP_NOT_ACCEPTED封存；两次启动均在
模型加载前失败（累计0生成/0负例/0模型加载）。原始包、失败记录和
上限保持原样，不解除retired或覆盖失败。持续工业化目标继续推进，
本阶段明确补执行此前未运行的完整业务出口。

## 本阶段冻结范围

候选仍为55cfae11源码构建的c02b9414；默认保持ecd5d21f/91ce0ed9。
先按SHA复用未变src/include/cmake/tests及完整构建配置对应的CPU
179/188、原生145+后端9、客户端27、120模板身份复用、5项生产
启动预检。新驱动仅增加独立阶段入口、累计额度与原始deadline保护，
针对性CPU检查后提交并冻结源码。运行时未变，不重跑上述已通过项。

计划19生成（含1个严格长度错误）/12负例/3启动；本阶段上限
22生成/12负例/5启动、首次launch起1800秒。额外2启动及3生成
只用于具体失败修复后的受影响项；无自动重试，不为统计波动补测。
此前两次失败在最终报告中单列累计，不能称为本功能首轮全部通过。

完整请求清单保持[原冻结清单](SERVE_THINKING_BUDGET_2026-11-03.md#http冻结清单cpu完成后首次gpu启动前)：
缺省/null、预算0/1/16/128、隐藏/JSON/SSE、自然闭合与禁thinking
前缀、真实严格资料工具往返、schema及总长度错误、MTP/plain和
私有SSD缓存seed/restore/fresh。每次启动先执行生产参数与目录
CPU检查，实际MTP批、回退、usage与metrics互相核对。

完整head、seq1、prefill2048；业务MTP/cache max_len与显式slot均
16384，plain/安装路径8192；只监听loopback。模型目录只读。一个
GPU lease持有完整业务、安装和健康阶段，竞争/监控失败ENV_ABORT，
只清理自己的进程。单严格租约释放后才发送下一请求。

通过完整19个出口和schema才配对替换默认，并在build/q4t真实HTTP
健康后、guard COMPLETED时finalize。失败保留原件和已验默认，安装
失败按已知pair回退；不改变通过标准，也不以内容展示代替真实选词。
业务任务无五档性能接受声明，记录必要质量与有界时延。

## 产物与执行

入口`tools/serve_thinking_budget/http_stage.py`；新产物独立位于
`.q4t-work/serve-thinking-budget-http-20261103/`。prepare记录原失败
身份、CPU复用身份、当前完整源码和source.bundle以及旧默认pair。
后续execute通过共享gpu_exclusive.py并使用已固定OpenAI3.42.1环境。
原工具stage.py保留原阶段历史，不用于再次启动已关闭阶段。

此项通过也只关闭SRV-05b；stop、产品预检、完整机器协议/JS SDK、
受载资源和工业化运营仍按总目标验收账继续。
