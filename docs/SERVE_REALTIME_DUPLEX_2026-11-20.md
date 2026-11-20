# Realtime双向音频输入与轮次所有权

接续rev376语音输出，属于Serve master A01/A02/A10/A12/Q01—Q03。
当前单独转写会话已执行ASR、降噪、VAD和语义轮次；对话会话已有
文字、工具与语音输出。待解决的是把音频输入接入同一conversation，
不是另启外部服务或让客户端桥接两个会话。总目标保持active。

## 合同与实现顺序

1. **有效设置**：在已有Prompt/default/显式覆盖组合后，复用严格转写
   解析器生成音频处理设置。公开转写可关闭，原生音频理解仍需执行；
   手动commit只提交输入，VAD的create_response和interrupt_response
   独立生效。server VAD空闲超时绑定输出完成与音频播放时长，不把
   生成时间当作播放时间。
2. **单一历史**：提交的音频有唯一item和generation，转写完成前不能
   伪造空消息或复用旧用户轮次。retrieve/delete、pending输入、迟到
   转写和实际音频缓冲都跟随这个所有者；容量预留覆盖子处理器和
   输出邮箱。客户端直接创建音频项也须经过真实处理。
3. **执行与调度**：复用现有原生ASR/VAD，不增加HTTP回环、第二socket
   或强制外部依赖。输入与模型执行分别记录实际工作、准入和结算；
   同一工作不重复扣费。自动回答等待输入准备完成，打断只取消所属
   默认响应，不能误伤旁路响应。手动response.create、取消后新请求、
   工具结果回填及空闲触发必须保持有界、有序且不自动重放。
4. **错误和恢复**：转写失败保留原错误和输入身份，不产生成功回答；
   ASR辅助失能不关闭健康文字服务。断连/删除取消所属借用，直到
   工作退出才释放缓冲；真实设备故障仍走既有保护。受影响恢复须
   保持输入/输出能力，不能回到只会文字的包。
5. **实际交付**：相关CPU、固定官方SDK及WSS的真实ASR/TTS短验，
   然后受影响主轻/恢复、可回退发行与合入。逐项复用未变后端证据，
   不开展完整性能/模型质量矩阵；听感与长稳维持用户延期决定。

## 固定来源与当前检查

使用已保存OpenAI JS 7.47.0的Realtime类型及项目统一schema。2026-11-20
核对[官方会话设置](https://developers.openai.com/api/reference/resources/realtime/subresources/client_secrets/methods/create)：
server VAD空闲超时为5000—30000毫秒，两个轮次控制默认true；
静音默认500毫秒。仅本对话规划器使用该值，既有转写端点的200毫秒
默认保持。本阶段没有引入外部程序，也没有复制第三方代码。

第一组是纯设置组件，尚不解除入口的未接线保护。冻结10项新CPU、
5项既有转写保护及20种来源反例，总120秒、0GPU。覆盖手动/VAD区别、
独立控制、转写关闭、真实处理参数、空闲边界、失败事务不改旧设置、
连续转写约束及include清除。实现和测试完成后继续第2—5项及全部
master，不把阶段组件测试当作完整双向接口可用。

## 设置组件结果

[双向输入设置CPU通过](evidence/serve-realtime-duplex-settings-cpu-20261120/README.md)：
15个不同用例/17次执行/0.533秒及20来源反例通过；原重复字段FAIL
保留，最终组件构建16.579秒零警告。入口接线/业务仍待完成，默认
rev376保持、0GPU；继续单一输入历史、ASR与轮次调度及全部master。

## 统一历史组件结果

[统一音频输入历史CPU通过](evidence/serve-realtime-duplex-history-cpu-20261120/README.md)：
30个不同用例/38次执行/0.495秒和20来源反例通过；原8项缺省字段
失败保留，最终构建16.485秒零警告。原生票据、ASR失败隔离、隐私
转写和快照已补，实际会话执行仍待接线；默认rev376保持、0GPU。

## 可组合处理器配置结果

[转写配置事务CPU通过](evidence/serve-realtime-duplex-configure-cpu-20261120/README.md)：
5新+10相关用例/0.597秒和20来源反例通过，host构建248.081秒零警告。
实际处理器校验与确认归属可组合，独立转写入口复用；原生双向事件/
轮次仍待接线，默认rev376保持、0GPU，全部master继续。

## 预留、冻结与原生生命周期结果

[提交前预留与待ASR上下文](evidence/serve-realtime-duplex-capture-cpu-20261120/README.md)
12新+30相关CPU/0.469秒通过；构建17.558秒零警告。
[原生生命周期](evidence/serve-realtime-duplex-owner-cpu-20261120/README.md)
11新+17相关CPU/0.896秒通过；host构建42.658秒零警告。两组均完成
20来源反例；0GPU，默认rev376保持。之前失败仍保留。

接下来把处理器与实际对话入口组合：单一公开session确认、输入事件
与公开转写过滤、音频检索/删除、等待输入的response.create、VAD打断/
自动回答和空闲触发。ASR工作快照保持不变，混合历史前驱由同一owner
在commit时确定；事件不能反向猜测或改变实际模型/输入归属。

## 输入事件组合结果

[组合输入CPU](evidence/serve-realtime-duplex-composition-cpu-20261120/README.md)
33个不同用例/34次执行均有有效PASS，20来源反例通过。原VAD夹具没有
到达故障路径的失败与2个来源SHA未刷新失败均保留；只补相应检查，
31项运行时未变的通过复用。最新host零警告，0GPU、默认rev376不变。

公开GA事件/隐私、精确输入删除和PCM检索已有组件证据，实际Chat入口、
等待输入的回答、设置冻结、VAD/空闲调度和直接混合音频输入继续实现。
输入管线失败不污染文字历史，但当前组件恢复新音频需重连，仍是待解决
问题；不能把组件隔离写成完整会话恢复通过。

## 实际会话owner与轮次结果

[会话输入与轮次CPU](evidence/serve-realtime-duplex-session-cpu-20261120/README.md)
53个不同用例/61次执行均有有效PASS，原8失败保留，仅补失败及新增
引用路径和直接wire保护。20来源反例通过，最终构建35.948秒零警告。
等待ASR的请求保留原期限和固定历史/设置，VAD打断不误伤旁路，
输入故障后的真实借用回收允许文字继续。固定SDK的新引用参数与旧
客户端总类型差异已在严格wire校验中接通，未修改原SDK图。

实际ChatServer尚未绑定输入回调；空闲超时、直接混合音频与同会话
输入管线恢复仍待实现，然后真实业务/受影响恢复/交付，全部master继续。
0GPU、默认rev376保持。

## 原生HTTP后端接线结果

[原生输入后端](evidence/serve-realtime-duplex-backend-cpu-20261120/README.md)
16个不同用例/18次执行有有效PASS，2项原夹具失败保留，仅补测试源；
20来源反例通过，host与实际q4t构建均零警告。ChatServer已连接共享
ASR/VAD和组合存储预算。配置ASR时默认手动输入，未配置ASR仍可文字；
VAD需显式选择，不能把后端失效伪装为已执行。

实际双向WSS业务尚待统一短验；先补齐空闲触发、直接混合音频和
同会话管线恢复，再受影响恢复/交付及全部master。0GPU，默认rev376不变。

## 同会话本地输入恢复CPU结果

[恢复CPU](evidence/serve-realtime-duplex-recovery-cpu-20261120/README.md)
18项和20来源反例通过，host零警告。实际join后复用同一处理器、预算、
历史和时钟，5秒冷却及固定60秒窗口最多2次；不重放、不重载模型。
真实设备健康仍保护。0GPU、默认rev376保持；真实WSS/ASR/TTS与新增
ChatServer健康门的runtime构建仍待完成，全部master继续。

## 空闲触发CPU结果

[空闲CPU](evidence/serve-realtime-duplex-idle-cpu-20261120/README.md)
26项和20来源反例通过，host零警告。回答终态加实际播放时长后计时，
真实静音/漏检样本经ASR；零样本不伪造ASR成功，空闲提示仅在原生
执行快照。旁路/失败不重置，清除/语音/故障/取消解除计时，恢复不
重放。0GPU、默认rev376保持；继续直接混合输入、runtime与统一
WSS/ASR/TTS短验、受影响恢复与交付，全部master继续。

## 混合音频历史CPU结果

[混合历史CPU](evidence/serve-realtime-duplex-mixed-history-cpu-20261120/README.md)
18项和20来源反例通过，host零警告。文字/音频顺序、逐段真实转写、
部分完成预算、隐私、失败与generation保护已具备组件证据；直接
原生音频处理及item/response.create入口继续，随后统一业务短验与
恢复交付，全部master继续。0GPU、默认rev376保持。

## 有限音频准备与队列CPU结果

[有限音频CPU](evidence/serve-realtime-duplex-finite-input-cpu-20261120/README.md)
22项和20来源反例通过，host零警告。PCM/G.711/配置降噪复用既有处理，
原生有限音频和append ring分开存储但共用总预算、队列和累计时长，
原借用/期限/删除与恢复边界保持。仍待实际item/response入口组合与
统一WSS/ASR/TTS、恢复交付，全部master继续。0GPU、默认rev376不变。

## 直接混合音频与单次回答入口CPU结果

[入口CPU](evidence/serve-realtime-duplex-mixed-entry-cpu-20261120/README.md)
36项和20来源反例通过，host零警告。实际item/response.create已接入
原生有限队列和历史；逐段处理、公开身份映射、整项检索/删除和
请求临时输入清理有针对证据，固定SDK参考transcript不进模型。
0GPU、默认rev376保持。继续runtime构建、真实业务/恢复/交付及
全部master，不能把组件验收扩大为最终可用声明。

## 真实验收与rev386交付

上述阶段时点记录保留；[当前交付](evidence/serve-realtime-duplex-delivery-20261120/README.md)
已将真实同会话输入、VAD/空闲/混合、语音与工具组合纳入主轻资格和
原生恢复/整包回退。服务STOPPED、客户预算和回退锚点保持。原错答、
环境中断、数值/声纹、检查器及命令格式失败分别保留，不追认成功。
完整证据见STATUS和交付索引；本阶段等待main合入，MCP/追踪/Live及
全部剩余任务继续，未宣称全模型质量或完整Realtime完成。

## 初始凭据更正与rev396交付

代理在恢复证据归档时误收私有配置；公开副本已删除，历史Git值
视为已披露。候选与保留包均重新签发初始值，5项认证CPU与19条
STOPPED管理命令通过，最终rev396。实际六次模型加载使用各自随机
凭据，客户Key/配额/用量与运行时保持；原业务按确切来源复用，
新封包新增GPU0。原失败不改写，旧值不得手动复用。更正、两向回退
和归档扫描见[凭据交付](evidence/serve-realtime-duplex-credential-reissue-20261120/README.md)。
