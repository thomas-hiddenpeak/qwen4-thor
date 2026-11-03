# SRV-10c共享缓存夹具修正

3743b4c8/候选7619966e的补验完成2原生HTTP、1模型启动、11生成HTTP
尝试：9生成成功、1取消、1预期quota拒绝。strict JSON立即工具调用/
真实read_project_record/引用回填通过，grammar3次回收；同ID和额度
再次通过。5次缓存生成全部成功，4个分支均正确回答43；跨client
私有缓存miss=0、同client私有命中8234已证实。

最后共享命中断言失败：alpha/beta共享只读请求均miss=0。代码与
README合同要求显式prefix_cache_write_tokens，原夹具始终省略，
默认0表示只读，故本次没有产生共享快照。原FAILED/COMMAND_FAILED
与全部请求/响应保持，不把它追认整轮通过；也不把正常只读miss
记为运行时隔离缺陷。此次生产代码、候选ELF和原驱动都不修改。

新独立驱动复用已验证的8258-token raw分支，固定8192写点，先alpha
显式seed，再beta用同scope显式seed（必须仍miss=0），随后alpha和
beta只读均须命中8192；四次答案均为43。最后普通SSE恢复必须710003。
不重复私有缓存、工具、取消或原生HTTP；新增1启动/6生成尝试/60控制/
600秒上限，预定5成功。4项离线驱动合同检查显式写入、只读边界、
错误命中拒绝以及原缺失seed证据，不能代替真实模型缓存验证。

本轮只验证剩余共享缓存与收尾，仍经完整同一GPU租约；WNOWAIT保留
模型/nginx到guard观察，核对额度/grammar归零、正常停服、GPU释放、
日志无凭据、默认与day身份。原失败不覆盖，不安装默认，不跑长稳。
