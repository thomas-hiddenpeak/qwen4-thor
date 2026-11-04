# 严格长输出：下一轮前提核对

当前结果：[严格复制候选实际背压限定通过](SERVE_STRICT_TRANSPORT_RESULT_2026-11-03.md)。
6生成/4负例/2启动首轮完成，默认保持、GPU释放；以下保留先验与执行冻结。

2026-11-03，基线57e0f28c、默认ed672a65/67897c81。
本文件记录已完成的离线先验及下方独立实际阶段冻结；不重启旧03b实验。
[旧失败及取证边界](SERVE_BACKPRESSURE_RESULT_2026-11-03.md)保持不变：
1次生成在1024-token上限内没有闭合，未到实际EAGAIN；没有生成IDs，
不能仅凭schema或canonical编码追认具体原因。

## 可证实的机制与候选

生产ConstraintTemplateRequest仅适配工具，response_format的schema
不进入渲染提示；原提示只要求“符合给定schema”。约束解码仍强制schema，
但模型无法从提示看到这份具体长内容。是否因此导致旧请求失败未知。

候选保持原schema、12443字节内容（48项，每项256等号）、max_tokens=1024
和request_timeout_ms=180000，用户消息明确给出完整JSON并要求逐字复制。
这改变模型条件，不更改grammar、解码算法或传输行为，不保证模型完成。

两个事实必须分开：Tokenizer.Encode的canonical序列可以很短；grammar
允许的其他合法分段可能很长。只有真实模型返回的IDs能说明实际路径。
当前grammar的空白有限（结构tag/schema设置16），不把失败归咎于无限空白。

## 本批CPU核对

入口[prepare.py](../tools/serve_strict_transport/prepare.py)，原生
[payload_probe.cpp](../tools/serve_strict_transport/payload_probe.cpp)。
只读模型tokenizer/config/generation_config，不加载权重、不调用CUDA、
不启动HTTP。使用已交付生产build的静态库，运行时源码/程序及依赖SHA
在前后核验；构建要求-Wall -Wextra -Werror，保留命令/独立stdout/stderr。

1. 原/候选JSON、SSE请求走生产计划、模板和分词；保存prompt/IDs和
   structural_tag，要求grammar逐字相同、候选完整内容在prompt中。
2. 真实词表（保留原始token字节）、实际EOS列表、隔离XGrammar worker：
   用合成有限logits强制重放canonical内容，检查每个token合法、EOS终止。
3. 独立worker重放同一内容前1100个单字节token，要求它们全部合法且
   此时EOS被拒绝。这只是反驳canonical长度上界，不代表旧模型路径。
4. canonical各token喂入生产parser，Finish前无内容delta；完整校验后
   JSON/SSE内容保持12443字节且合并一份delta。保存实际MessageJson和
   SseDeltaChunk字节；截断前缀不得释放内容。未触碰socket，不称HTTP通过。

两worker，最多约1345次Select，单worker60秒合作截止、原生进程150秒
外部上限；不额外尝试其他payload/扩大输出预算寻找通过。首次失败保留，
仅针对具体实现/夹具失败做受影响重验。产物新目录，旧证据只读保留。

## 下一实际阶段的必要条件

CPU可行不等于严格背压接受。先验完成后须补齐有界生成token取证（仅
验证程序显式开启、不向生产日志泄露内容），再冻结独立真实阶段范围、
预算和失败出口。若下一尝试仍提前失败，保存原wire与生成IDs做具体定位，
不能只增加max_tokens、换名字或重置旧实验预算。

实际接受仍需要完整strict校验后的真实TCP EAGAIN；严格SSE取消、严格
JSON写截止、排队普通请求及strict恢复、控制可用与资源回收分别证明。
普通流已有证据不能替代；新阶段全程GPU锁，当前默认不变。


## CPU结果与独立真实阶段冻结

CPU首轮通过，原生probe07d739d1，使用生产ed672a65静态库。
实际vocab248320、可解码词表248077、EOS=[248046,248044]；原提示21token，
显式复制候选289token。canonical内容243token全部通过mask并接受EOS，
1100单字节token前缀全部通过但EOS拒绝。2个worker均reap，分别0.221/
0.341秒；这是合成logits可行性，旧模型失败原因仍未知。
JSON/SSE Finish前均无delta、校验后单内容delta12443字节；MessageJson
12574字节，固定测试封套SseDeltaChunk12751字节，截断不释放内容。

验证程序新增Q4T_SERVE_GENERATED_IDS=<request_id>：只对指定strict请求、
在生成后/响应前记录真实token，最多4096个，显式total/captured/truncated。
生产callsite由故障宏完全排除；开关关闭/错请求/正确请求×ON/OFF共6项
CPU通过。运行时源码仅chat_generation.cpp和serve_fault.h变更，没有
修改模型、模板、分词器、grammar或parser。默认ed672a65/67897c81保持，
不安装故障程序。专用程序零警告构建，协议身份因源码观测点变化另记。

驱动首次9项中8通过、1项缺socket import失败；原驱动/日志保留，修复后
仅该项重验通过，其余8项复用。涵盖固定预算/guard、坏token记录、EOS及
长schema内容判定、空缺证据、生产请求4形态、队列清理、真实写阶段顺序
的模拟检查；模拟结果不计实际HTTP通过。

这是基于具体候选条件变化和新增取证的独立阶段，**不是旧03b更名重跑**：
保持同一输出要求与1024-token上限，提示从没有具体内容变为完整复制目标；
真实完成与否可由实际生成IDs核对。旧实验仍1生成/1启动未通过，不重置其
预算/期限，不补写不存在的证据；本独立阶段也无自动重试/加样预算。

真实冻结：**6生成尝试、4专门负例、2模型启动、首次guard起1800秒**。
两个专用plain单槽进程，8192上下文/2048prefill，loopback18104；每组长
strict、排队普通710003、短strict恢复各一次，strict忙拒绝与错误取消凭据
各一次。SSE在真实EAGAIN后正确取消；JSON保持客户端不读直至30秒写截止。
两组均核对生成IDs/schema及>12KiB内容、部分原wire/客户端拒绝、无成功尾帧、
first-write handler原因与trace/指标、队列/strict/worker/body/slot归零和reap。
health/ready不占生成预算；不先读取长响应解除背压。request deadline180秒，
必须先于它完成写截止；握手仅10秒，不能用握手暂停时长代替真实EAGAIN。

完整阶段同一GPU独占锁；竞争/监控失败ENV_ABORT，只停止自己的进程。
首次失败立即停止后续模型请求，已结束请求保存wire；其他失败先结束
自有服务，再有界接收同一连接前缀。生成IDs在响应前已有落盘日志，可用
于具体定位，不重放模型。既有普通工具/生产健康及数值结果按未变依赖复用。
记录请求质量、生成/背压耗时及稀疏parent/worker/全局内存，不作性能或
整机RAM上界声明。若通过，仅交付限定证据、工具与文档，默认程序不替换。
