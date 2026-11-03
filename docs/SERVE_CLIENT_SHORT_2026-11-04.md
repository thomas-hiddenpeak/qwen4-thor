# SRV-10c真实客户端隔离短验冻结

业务可靠性任务，非性能优化。已开发候选5487bc67及其27项原生HTTP
生命周期binary，280公共/246必需host、9后端、28 SSD检查点和70包
回归已有证据；原独立target漏链失败保留，构建修正后零警告。

本短验只验证直接runner加真实TLS网关，不冒充托管包实际部署或
新ELF长稳。最多2次模型启动、22次generation HTTP尝试（含未生成
的负例）、120显式控制、600秒全阶段。预定12实际生成，其中11成功、
1显式取消，1真实只读资料工具。没有自动重试或追加采样。

1. 同一GPU租约内运行27项原生HTTP生命周期合同，包含真实入口身份、
   同ID取消隔离、body/并发额度、异常回收；空模型不执行CUDA。
2. 未启用policy的候选default参数一次普通质量请求，正常关闭。
3. 同候选启用2客户端私有策略、MTP k3、seq1/16384明确容量与私有/
   shared缓存；实际nginx/本机验证证书/SNI。固定alpha512输出/12000
   输入，beta64输出/9000输入，逐client data1/control2；网关data4
   使runner额度能够独立触发，全部仍受全局边界。
4. 后端缺失身份401；chat/preflight输出超额与实际模板输入超额一致
   400且前者不得先SSE。省略输出预算beta64；外部伪造内部身份头仍
   按真实alpha Key执行。相同request_id的alpha/beta同时存活，beta
   即使持有alpha取消token也404；data满时control可用，自身202取消，
   另一client成功终态。拒绝与取消后普通请求/严格JSON恢复。
5. 真实只读`docs/MODEL.md`工具调用、执行、结果回填及引用回答。
6. 固定8200个` x`的raw模板预先检查真实tokens在8192..9000内。
   alpha保存私有+shared；beta以相同key完整续写必须miss0，alpha
   私有恢复必须>8192；两client随后shared-only均命中8192且回答43。
   首轮8token长度终止，后续不扩大预算或调整prompt以取得通过。
7. idle复核全部请求/grammar/client额度回收，停止自有nginx/模型，
   WNOWAIT保管四个直接子进程到guard新观察，核对GPU空与原lock。
   检查日志没有内部共享token或外部API Key，默认pair和两个day计划
   身份/未执行状态保持。轮询不能排除采样间隙外部干扰。

原始私有policy/API Key/私钥/nginx.conf禁止入Git。driver先做4组离线
夹具/oracle校验，再commit绑定来源、prepare，只有外层gpu_exclusive
持有原锁时run。发现竞争/监控失败记ENV_ABORT，仅清理自有进程。
失败保留现场并仅修复/重验受影响项；不启动旧vLLM、小时或day验证。

通过本短验后仍需实际托管包绑定与有界部署交付；后续继续物理RAM、
实际LAN/证书/防火墙、正式跨主机通知和日志跨代次边界。不得把本地
范围通过当成整体工业化目标完成。
