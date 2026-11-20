# Live 原生应用服务实现计划

总目标保持 active，沿用用户确定的完整推理/应用服务范围。组织管理、
云账单和微调训练不纳入；Live 的媒体、会话和委派属于应用服务。
本阶段编号用于实现/验证/提交，不是总任务终点；所有剩余项目继续。

固定依据为本地官方 OpenAI JS 7.47.0、TypeScript 5.9.3，以及2026-11-21
保存的官方 Live 指南。sources.json记录19个SDK来源（含MIT许可）；
类型图16个根、941个节点、7个实际可达来源，SHA256为
c145a8561370cf58536e63ef941b8fe3a39c0b147f7aa9a136282b11632e81d6。
图只作为输入/输出合同，不能代替执行。运行时不加载Node/TypeScript/SDK。

| 子项 | 实现和出口 |
|---|---|
| L01 合同 | 复用C++有界类型校验器；完整主连接、sideband、fork、HTTP媒体/会话类型根；独立SDK编译器正反例；重复字段、深度/工作量和输出错误边界。还不宣称业务可用。 |
| L02 主WSS | /v1/live/sessions原生握手、鉴权/会话槽；session.start/started和不可变设置；持续PCM16/24k与G.711输入、真实ASR/LLM/TTS、音频/转录增量、mute/unmute、正常排空/异常断开。累计音频秒与后端token分别计量。 |
| L03 任务与上下文 | frontend/back-end独立指令与上下文所有权；client/Responses/Decisions委派、响应包装、工具回填、触发器与取消；静默/可说上下文、提示追加、初始历史、确认延时和音频fixture。所有模型工作受现有RPM/TPM、公平准入和资源预算约束。 |
| L04 存储与旁路 | 认证scope绑定的sideband、可选store、录音下载、完整历史fork及所有者切换；每连接权限和背压独立；辅助失败不得停止健康核心；不重放旧工具/请求，不伪造已关闭事件。 |
| L05 媒体传输 | 原生WebRTC及SIP会话接入，create/accept/reject/hangup/refer、fork/sideband与控制权限；先审核已有必要系统传输/编解码能力，新增库需说明启用范围和失效影响，nginx/systemd及外部媒体服务不得成为核心强制依赖。真实本地对端短验，外部电信账户结果与本机合同分开。 |
| L06 组合交付 | 标准SDK双向语音与真实项目资料工具闭环，主/轻配置、入口/辅助/核心故障边界与资源清理；可回退包、文档/矩阵、CI、main。旧模型质量失败/听感待验保持；继续所有master缺口。 |

首组仅成组接入L01，冻结4项新原生CPU场景（其中1项对照808个独立SDK正反例）及
2项直接受影响的既有Realtime校验场景。完整SDK图不宣称执行业务通过。
不运行GPU、不做性能宣称。L02/L03接通后冻结对应真实业务短验预算，
按同一严格GPU租约运行；不重复已验身份不变的完整质量/性能矩阵。
协议事件必须来自实际状态、调用和计量；socket关闭不冒充成功排空。

模型适配：复用原生ASR、当前LLM及原生TTS，frontend与delegated backend
可共享物理模型，但保持独立提示、历史与准入。不伪称具有GPT-Live模型
权重或原生端到端语音模型的时延/质量，不能把该区别用作不实现接口的理由。

引用：
- https://developers.openai.com/api/reference/resources/live
- https://developers.openai.com/api/docs/guides/live
- https://developers.openai.com/api/docs/guides/live-conversations
- https://developers.openai.com/api/docs/guides/live-delegation

L01源码已接入工作树；6项针对性原生CPU通过，Live 808与Realtime 476个SDK正反例无差异。首轮独立链接漏掉既有JSON转义函数，原失败保留；补齐链接后通过，运行时未为此改写。尚未接入服务路由或模型业务，默认rev426保持STOPPED，未运行新GPU。PR #57已按冻结head合入main，本组单独提交。

正式宿主构建与生成器一致性已通过，原始结果见[合同CPU证据](evidence/serve-live-contract-cpu-20261121/README.md)。下一组接入独立Live配置/生命周期及实际语音，不把现有Realtime的24k输出限制误用为Live的16k/24k合同。

配置事务9项、16k转换与直接保护12项CPU通过，见[原始证据](evidence/serve-live-configuration-pcm-cpu-20261121/README.md)。真实计数/后端绑定仍在后续L02实现，不能用受控CPU回调宣称模型业务通过。默认原生声线将按实际名称公开，不能把vivian冒充marin等其他模型声线。独立会话关闭/排空、持续输入和真实工具链继续。
