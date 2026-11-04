# SRV-10a本机TLS与双客户端业务限定交付

已完成用户确认的接入边界开发：受控内网、TLS网关、独立API Key。
本批在Thor **loopback**完成真实TLS和模型短验，未开放LAN或安装常驻
网关。入口为[prepare/check/run](../tools/serve_gateway/README.md)；
默认runner d66f2bb9、sidecar42d7672a、protocol55a3e67b及211来源不改。
实际内网部署、长期运行和完整工业化接受仍未完成。

## 交付与证据边界

| 能力 | 本批实现与实际证据 | 仍未证明的范围 |
|---|---|---|
| TLS与客户端鉴权 | nginx1.30.6官方签名包，TLS1.2/1.3；两独立Key，缺失/错误/大小写/前后缀/重复头拒绝，证书信任与域名失败验证 | 实际内网CA/DNS、证书轮换运营、远端客户端与防火墙 |
| 路由与运维隔离 | 5个显式业务路由与OpenAPI逐项核对；config/metrics/健康端点仅后端loopback，query与路径别名拒绝 | 不可信本机租户隔离，远端绕过入口验证 |
| 配额与控制通道 | 按Key分data/control并发与速率，429带Retry-After；alpha满额不阻止beta排队或alpha取消 | 全局8请求饱和、多客户端长期公平性、token计费；不是多租户存储隔离 |
| JSON/SSE/工具 | 完整JSON/SSE、strict JSON、真实项目文档工具读取及带引用回填，响应身份保持 | 所有模型任务/SDK/多模态质量；不是全OpenAI API兼容 |
| 取消与失败恢复 | 取消202，完整explicit_cancel+[DONE]，不伪造finish/usage；排队client成功及取消后恢复；断连向上游传播 | 任意实际网络/长等待/慢客户端条件下的表现 |
| 运行与审计 | 只在READY且固定ELF/同namespace/loopback后启动；私有包校验、简化日志、显式停机换Key、无自动重试/重放 | 持续后端身份监控、零停机换Key/撤销在途请求、常驻有界日志和通知 |

允许GET `/v1/models`、`/v1/capabilities`，POST `/v1/chat/completions`、
`/v1/chat/preflight`、`/v1/requests/cancel`。preflight计入data，发现与
取消计入control。API Key和转发身份头不进入后端，网关与runner各自
保留请求关联ID；网关错误使用gateway_代码，上游业务错误原样转发。
取消仍按runner的request_id+cancel_token凭据验证，API Key并不替代它。

真实短验采用data每client1/全局4、100r/s/burst128；传输夹具另使用
2048字节体上限和1r/s/burst1验证拒绝，真实模型采用16MiB。产品默认
data每client2/全局8、5r/s/burst10；默认数值的完整饱和没有实际压测。
慢头/TLS握手不纳入每client完整请求配额，另受连接与超时上限约束。
本机用户/管理员属于可信边界，代理不声称完整DoS保护或公网适用。

## 首轮失败与定向修复

[原合同](SERVE_GATEWAY_2026-11-03.md)及
[修复补验合同](SERVE_GATEWAY_CORRECTION_2026-11-04.md)均保持原件。

1. 原配置CPU9项首轮4通过/1失败/4错误。错误检查插入位置、nginx默认
   temp目录、syntax-test空PID文件及umask问题依次修复，定向执行
   9+5+3+1+1共19次后9有效通过。早期TemporaryDirectory清理了3份
   原nginx失败目录，原测试输出/源码仍在；后来的syntax-diagnosis是
   独立复现，不冒充丢失原件。之后全部私有目录保留。
2. add0d459传输夹具35尝试/2网关启动/11上游stub请求通过，1.180秒，
   独立guard COMPLETED。**其中取消路径与代理同错**，此绿色记录
   不能证明真实runner取消，后续真实模型才揭示问题。
3. 60b9b801模型首轮1启动/7生成：5成功（含真实工具），cancel误走
   `/v1/chat/cancel`返回404。驱动等待线程池；核对自有controller后
   额外正式stop，活跃和排队2请求获得server_shutdown。原结果FAILED
   不改，未完成原计划第8次恢复。原6控制+独立stop1单列，GPU153.290
   秒/总153.424秒，正常停止/guard COMPLETED并清理。
4. e252969f修复取消路由；prepare/check核对正式OpenAPI并绑定SHA，
   防止代理和stub互相证明错误路径；驱动失败先停自有后端再join。
   4项定向CPU首轮通过（1重验+3新增），8项未变配置与4解析/3隔离
   入口复用。配置/回收共12有效case、累计23次实际单测执行；不将
   原失败抹掉。独立补验只执行受影响3生成，原5成功逐wire复核复用。

新旧完整nginx.conf在内存比较，仅取消路径和私有包目录变化；renderer、
TLS、鉴权、配额、转发函数及后端保持。私有diff未导出。

## 实际补验与质量

冻结e252969f，后端复用已接受v3包160796c3；网关新包manifest
`87e2da423e7e5086ccd5e3d16ebfbdee60218504cbc68c56368e1ca6353de7c5`。
本机自签夹具证书只作验证，不应作为实际部署证书。

- 补验1启动/3生成，2成功/1explicit_cancel。alpha活跃时beta实际
  排队，alpha超额429；独立control取消202，活跃流完整关闭，beta
  返回710003，alpha恢复也返回710003。旧错误路径由网关404拒绝。
- 原strict响应为`{"ok": true}`。真实`read_project_record`访问
  docs/MODEL.md第1..24行，SHA匹配真实文档，模型回答
  Qwen4ExpForConditionalGeneration并引用[docs/MODEL.md:L1-L24]。
  归档工具结果与文件内容逐行核对，没有重做工具执行。
- 原5成功时grammar3/3回收；补验最终3总/2成功/1取消/0错误、owner
  归零、slot释放，无新grammar。累计模型2启动/10生成：7成功、1显式
  取消、2首轮停机错误，原预算不追认为通过。
- 补验GPU38.624秒/总48.830秒，6显式控制；STOPPED/COMPLETED，正常
  停止后10秒无复起。两轮guard分别142/37观察，原日志覆盖完整租约，
  无观察到竞争；不能排除短于采样间隔的干扰。GPU窗口累计191.914秒，
  显式控制累计13；监督GET保守上界原492/补验252另列，未当实际计数。
- 单次普通响应0.326..0.437秒、strict0.605秒、工具调用3.730秒与
  回填4.021秒；补验排队成功0.507秒/恢复0.325秒。只记录本批观测，
  不是性能优化、吞吐非劣或五档E2E结论。

## 封存与交接

[330份公开原件与身份目录](evidence/serve-gateway-20261104/manifest.json)
封存9份Git文件724888字节，68份本机私有材料/二进制仅记身份；完整
wire、原失败、CPU来源、真实传输、官方签名链和生命周期记录保留。
API Key、private key、nginx.conf与私有syntax诊断不进入Git；逐项
扫描所有本批已知Key并核对归档内容。证书未来过期不否认历史握手，
实际再次运行仍须经生产入口校验当前有效期。

只读复核入口（不产生模型/HTTP）：

```bash
python3 -B tools/serve_gateway/report.py verify-archive
```

封存过程的docker权限失败、另一指令任务随后占用原锁、消失PID读取
异常各自留记录，未重跑业务或干预其他任务。docker只读查询采用
既有sudo权限，PID不存在按已退出处理；最终GPU空/原锁可用、两轮自有
进程全退、容器空、SOAR仍masked/inactive。后续其他会话可正常获取
GPU；此记录不声称机器会一直空闲。

默认pair未替换，用户[SRV-18 day计划](../tools/serve_soak/README.md)
只读身份检查通过且execution不存在，长验由用户专门开展。
实际LAN上线还需确定绑定IP、DNS/证书信任、客户端清单/配额与来源
限制，并接入常驻有界日志/告警；工具已给出私有配置及显式运行入口。
物理RAM硬保护、实际通知与小时/天级稳定仍在工业化验收账中待办。
