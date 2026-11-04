# SRV-06 文本停止交付

2026-11-03 03:56 CST完成验收、默认替换与资源复核。SRV-06按有限
profile交付，长期“完善serve到工业水准”目标继续active；下一项SRV-07
请求预检与有效配置。开发合同、原始计划和执行分组见
[文本停止合同](SERVE_TEXT_STOP_2026-11-03.md)。

## 当前默认与合同

- ELF：`a3fd64a34a67f619bf2978faf152d6f455652d048a1e7a2cdc48caf784898184`。
- sidecar：`49c7c63159c806d8b8f28d5a5a46e5f1c2b6562cc608f46bc43d921c48d0d35a`。
- 运行时实现ea2dbcfb；最终冻结驱动/源码85e309c1。后续入口和夹具
  修正均未改变运行时。旧31ffc41e/9a71a0a7和完整回退链保留。
- 本机Thor、完整head、单槽8192、prefill2048；候选默认MTP k3，
  实际安装健康用`build/q4t --no-mtp`。模型与数值源码未改，不作性能接受。

`stop`支持单个字符串或最多4个非空UTF-8字符串，每条最多256字节；
缺省/null/[]禁用。对生成文本先做现有UTF-8修复，再在响应解析前匹配，
包含推理/工具标记；不匹配prompt，也不做Unicode规范化。最早完成者
优先，同一完成位置按列表次序。JSON/SSE共用增量缓冲，停止串及尾部
不发布；未命中的尾部在EOS/length时释放。最多保留255字节停止前缀。

usage计至触发停止的实际token，含停止串占用，不按可见文本重新分词。
MTP仍完整提交当前批KV，逻辑停止后退休整份状态；同批后续token不发布、
不计completion，但不声称省去了这些已发生的计算。命中优先于同token
长度界；普通文本finish为stop，已完成工具保留tool_calls终态。

推理内主动停止可返回不含答案的推理前缀，隐藏推理不改变匹配/usage。
普通auto工具中断仍为response_parse_failed；不得执行部分调用。
有效stop与strict/required/named/parallel=false/JSON约束、state cache
组合在模型工作前400拒绝；中性stop不引入这些拒绝。此范围不是所有
OpenAI端点、任意组合、媒体或完整SDK兼容。

## 实际业务与终态

| 检查 | 实际证据 |
|---|---|
| 英文跨token JSON/SSE | 完整ALPHA0123456789ENDOMEGA为16token；stop=456789END返回ALPHA0123、13token，两个传输文本/usage一致 |
| MTP同批尾部 | 上例实际4步/15接受token，逻辑停止后2个token不发布；raw模式相同 |
| 中文 | 开始中文停止之后在中文停止处返回开始；3token，MTP同批后续1token不发布 |
| 重叠 | ababa与bab按最早完成选bab，返回ALPHAa；与请求列表中前项不同 |
| EOS/未命中前缀 | 完整生成数作为max时仍stop；ALPHAEN配END完整释放未命中EN |
| 单token竞争 | stop=7、max1返回空文本/stop/usage1；stop=7X返回7/length/usage1 |
| 推理内停止 | 在close标记前停止，正文空；公开推理为原思考前缀，隐藏版不暴露该字段，二者usage均18 |
| 预算与stop | 预算0与原生换行/close/空行停止串组合，completion3/forced3；没有生成答案，计量一致 |
| 工具截断JSON/SSE | 在第一个参数close前停止；两次都error，无成功finish/usage/可执行调用；MTP同批后续3token不发布 |
| 真实工具闭环 | auto调用read_project_record读取MODEL前8行并回填，最终回答含Qwen3.8-Flash-Next及[docs/MODEL.md:L1-L8]原citation |
| 中性stop与schema | []不改变严格JSON/预算路径；完整answer=710003对象成功 |
| 实际安装plain | 新build/q4t --no-mtp加载后JSON/SSE及健康3次返回ALPHA0123，prompt50/completion13，与MTP完整message/usage一致；实际MTP步骤0 |

输入16负例均在SSE首帧前400，参数位置明确；token/worker计数不增长。
37份接受范围响应/143个封套通过transport schema，18份成功SDK响应中
111个message/delta通过reasoning投影，18份usage通过thinking动态等式，
21请求通过stop投影及UTF-8字节限制检查。raw和截断工具使用实际HTTP
封套单独检查，不能把这些用例说成完整官方SDK请求支持。

MTP停止示例0.556/0.559秒，实际安装plain JSON/SSE为0.887/0.791秒，
项目工具回填回答2.279秒。这些是有界业务时延，不是五档对照或性能非劣。
每组活动chat、严格租约、worker与词表storage归零，全部自有服务正常关闭。

## 原始失败、修正与额度

1. 首次guard调用用了相对脚本路径，被祖先身份保护拒绝；0模型/0请求。
   新launch构造绝对argv，未放宽保护。
2. 绝对入口误用系统Python，模型加载后缺openai；1模型启动/0请求，
   正常关闭。绑定已验SDK3.42.1环境，并在取锁/模型启动前构造真实客户端。
   SDK依赖使用httpx2，CPU预检也不再假设httpx包名。
3. 16负例和7生成通过，第8次长度夹具把710误认为单token。实际首token
   是7，服务正确length。修正夹具为7/7X，运行时不变；原错误响应保留。
4. 复用同ELF的7个通过响应和16负例，最终新增11个MTP请求与3个安装
   plain请求通过。普通解码与安装健康合用一次加载；安装MTP使用相同ELF
   的候选证明，不称安装后另起MTP，也未删除任一普通解码/工具检查。

累计**22生成（含夹具失败1次）/16负例/4次模型启动**，4次guard入口
中3次COMMAND_FAILED、最后COMPLETED，ENV_ABORT0。21有效用例由7个
有效复用及14个新请求组成。最终执行84.06秒；首次入口到finalize
833.00秒，保持原30分钟/22/16/4上限。不是首轮全部通过。

CPU OFF188/ON197各有一项工具夹具前缀错误；原件保留，修正后两种
构建各仅重验该项通过。生产154必需合同、9个真实grammar、客户端28
通过；驱动14项中离线stub补齐后仅失败项重验。初次range-loop警告
保留，最终生产构建零警告。新文件空白整理保存前后SHA及等价记录；
模板120与启动预检按依赖身份复用，不称为新跑完整模板/数值矩阵。

## 证据、回退和剩余工作

原始材料见[证据入口](evidence/serve-text-stop-20261103/README.md)，本地
`.q4t-work/serve-text-stop-20261103/`保留全部4套包和4次入口。未修改
模型目录、reference或旧接受包；大ELF与source.bundle仅登记本地身份。
直接复制530份记录，归档含说明/身份/seal共534文件、约4.15MB，
22个大产物保留本机身份；根seal覆盖原字节，Git索引再核对一次。

回退用冻结85e309c1源码、保留原绝对路径，并在新的GPU guard下运行
`tools/serve_text_stop/stage.py rollback --fixture-retry --output ABS_DIR`。
来源漂移/未知pair拒绝；配对的两文件逐个原子替换，不声称跨文件断电事务。

03:56:31 CST GPU计算PID空、共享锁可用、恢复标记空，无常驻模型；
MemAvailable约116.34GiB，未主动丢弃页缓存。下一项SRV-07，随后完整
机器请求协议/JS SDK、受载资源、严格长payload背压、访问运维与稳定运行。
