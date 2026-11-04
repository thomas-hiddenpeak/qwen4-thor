# SRV-10c终态清理交接修正与剩余短验

首轮8df35dd7/候选5487bc67已执行：27原生HTTP通过、2模型启动，
10 generation HTTP尝试中4成功、1取消，其余5在生成前拒绝。
缺失鉴权、输出及输入预算、preflight、省略输出、伪造内部头、同ID
隔离/跨client取消拒绝、data饱和/control可用、自身取消及恢复均通过。
strict JSON成功后立即project-read收到429，真实工具0、缓存0，停止
并清理；原short/result为FAILED、guard为COMMAND_FAILED，不能追认通过。

原因是客户端data额度保持到grammar/output清理完成，nginx按正文
长度结束响应，下一次请求可能先于上一handler析构到达。不能把
response EOF当后端清理证明，也不能提前退额或提高额度掩盖问题。
修正按已有structured handoff原理，在任何终态字节可见前标记
finishing；仍保留原额度到ClientLease析构。新请求只在同client同lane
有finishing所有者时最多等待100ms，真正active满仍立即429；等待
不会增加额度。跨client/control独立，等待者仍占全局有界连接。

3新增host合同：finishing仍计额、释放后才能接替；100ms超时仍拒绝且
不退旧额；生产HTTP BeforeTerminal触发交接。重验15个client合同和
受HttpResponseContext影响的server_http合同，off/on两构建；其余
未变host、实际SSD和70包结果按身份复用。旧build及ELF不覆盖。

实际独立handoff-build零警告、来源不变，候选7619966e；29项
client/server_http在off/on分别全部通过，inventory通过。原4项
短验驱动合同对应的short.py/test_short.py未改，按身份复用。

补验冻结：最多1模型启动、15 generation HTTP尝试、100控制、600秒。
同锁先重验2个client原生HTTP合同，随后重复受限额改动影响的同ID/
取消/data饱和，strict JSON立即接真实工具read/回填，中间不sleep、
不idle、不retry、不增加client额度。继续未运行的5次真实私有/shared
cache隔离与普通恢复；预定10成功+1取消、真实工具1次。输入/鉴权负例
与legacy质量按首轮已通过且相应代码未变的证据复用，不重复完整矩阵。
模型数值、模板、tokenizer、cache存储算法与业务测试请求均不改。

沿用同一私有策略/证书/网关包，固定新候选和protocol摘要。阶段内
WNOWAIT保管native、模型、nginx三个直接子进程到guard新观察；记录
退出/GPU/锁、日志秘密扫描与default/day身份。失败保留并仅修正
具体问题，不执行长稳；本轮不安装默认，托管部署/发布继续待收尾。
