# SRV-10a取消路由定向修复与独立补验

原合同见[SERVE_GATEWAY_2026-11-03.md](SERVE_GATEWAY_2026-11-03.md)，不
追改冻结预算或历史结论。源码60b9b801首轮实际模型短验失败：网关与
传输stub都误用了`/v1/chat/cancel`，runner实际端点为
`/v1/requests/cancel`（chat_http.cpp及正式OpenAPI一致）。原夹具绿色
只说明其自身透传，不证明真实取消接口；模型取消返回404发现了缺口。

首轮1启动/7生成已执行：5成功（JSON、SSE、strict、真实资料工具与
引用回答），活跃与排队2请求经正式stop获得server_shutdown，未通过
原取消场景。原2网关拒绝成功。驱动线程池在控制失败后先join，导致
等待长输出；在核对controller PID/start/boot后独立正式stop了自有
服务，额外1控制操作单列。原驱动6控制，总显式7；153.290秒GPU窗口、
153.424秒总计，STOPPED/guard COMPLETED/cleanup_confirmed，GPU空、
原锁inode3294067可用。原result仍FAILED，未冒称原8生成计划通过。

## 修改范围

- 修正gateway、transport夹具和模型驱动的取消路径。生产prepare和
  check将全部5个允许的路由/方法/control-data分组与正式OpenAPI
  operationId交叉核对，并在包manifest绑定合同SHA；管理接口不可
  因存在于OpenAPI就自动开放，已有包也不隐式升级。
- 驱动在并发块内失败时先正式停止自有后端，再等待请求线程结束；
  预留清理不被业务次数上限阻止，原始失败与清理失败分开保留。
- 新CPU组4项：已有真实nginx私有包case重验，错误路径/方法/分组/
  缺少路由/管理泄漏拒绝、合同身份漂移拒绝、失败先stop再join。
  其余8个网关case与4个取消解析CPU检查按未变函数身份复用。

renderer函数、TLS/key/配额/转发实现保持；新旧真实nginx.conf只允许
取消路径和私有包绝对目录改变。在内存比较完整配置，不输出含Key
的diff。旧5个成功wire重新解析且与原response完全匹配，后端/数值/
模板/业务工具身份不改，复用这5个成功；不重做工具执行。原fixture
控制端点不能证明真实runner取消，改用以下实际后端定向验收补齐。

## 独立补验预算

`tools/serve_gateway/correction.py cpu/prepare/check/run`，新原件位置
`.q4t-work/serve-gateway-20261103/model-corrected*`，不覆盖首轮。
CPU与实际依赖提交后冻结；新网关包沿用同一夹具证书与两client凭据，
不安装系统服务、不开放LAN、无自动重试。实际模型同一生产guard租约
贯穿完整补验，不额外嵌套租约。

- 最多1次模型启动、3生成：alpha活跃SSE预期explicit_cancel、beta
  排队普通请求成功、alpha取消后恢复成功。新增预算不是原计划剩余
  次数；累计最多2启动/10生成，5旧成功+2新成功分开列账。
- 2负例：旧错误路径在网关404且不进入模型，同client并发429。
  取消202确认、流完整error+[DONE]且不伪造成功；control可在data
  饱和时通过，排队client正常完成，最终3总/2成功/1取消/0错误。
- 显式控制≤96（含清理保留8）；GPU≤300秒，总≤360秒，生产监督
  GET保守上界252另列。正式停服后10秒不复起，所有自有进程、GPU
  和原锁回收；采样不能排除短于采样间隔的外部干扰。

只修已发现问题及补验受影响项。失败保留现场，不自动重跑/扩样。
不改默认d66f2bb9/42d7672a或SRV-18 day冻结依赖，不启动用户长稳。
本机TLS短验通过仍不等于实际内网证书/防火墙/多机接入、常驻日志/
通知、物理RAM硬保护或长稳通过；持续工业化目标保持active。
