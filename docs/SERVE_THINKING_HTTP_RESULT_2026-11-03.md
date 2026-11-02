# SRV-05b 真实推理预算交付

2026-11-03 03:11 CST完成限定范围验收与默认替换，03:13完成环境
复核。**SRV-05b已交付，工业化长期目标继续active。** 本结果包含
真实失败及修正，不称为首轮全部通过，也不代替stop、产品预检、完整
客户端矩阵和受载运营验收。

## 身份和实际控制合同

- 默认ELF：`31ffc41efefe7e407a0848041e03673906e8965865b9464019aa54a99fd5dee6`。
- sidecar：`9a71a0a72e76da3b3850a340fb30b882da633a867e51bf454bee92ba9f5f9a72`。
- 运行时及本次验收驱动冻结提交：97e2f573；原实现55cfae11的候选
  c02b9414保留为未接受版本。旧默认ecd5d21f/91ce0ed9和完整回退链保留。
- 本机Thor、完整head、seq1/prefill2048；MTP业务及私有缓存16384
  显式槽，plain与安装路径8192。严格输出仍plain masked greedy。
  模型、数值算子和MTP计算源码未改，不作性能优化接受声明。

`thinking_token_budget`仅用于parsed messages；缺省/null关闭，非null
为[0,2147483647]精确整数。累计自然生成token开始或进入推理区的次数，
自然close计入，EOS/prompt前缀不计；多个推理区共用额度。耗尽时在
真实选词路径插入原生`\n</think>\n\n`边界，已有部分close就补其
后缀和空行。UTF-8未完整时先有限补齐；控制token最多14，实际本批
原生完整边界为3。之后禁止再次完成新的推理开标签；工具/JSON中的
字面标签保留。控制token仍占总输出额度，不能借此超出max_tokens。

MTP余量不足时在批次提交前回退，保留合法pending；不截断已接受KV。
grammar先与强制token求交再接受一次；不能先改matcher后换输出。
`completion_tokens_details.reasoning_tokens = consumed + forced_closure_tokens`，
`usage.q4t_thinking_budget`显式记录limit/consumed/forced_closure_tokens。
强制token包含边界空白；隐藏输出不减少这些计量。预算不保证模型
所有正文都没有推理语义，也不是FLOPs或未接受speculation计算上限。

## 失败、修复与累计额度

| 执行 | 实际结果 | 保留及复用 |
|---|---|---|
| 上一开发阶段 | 2次模型加载前CLI/目录失败，0生成 | 原retired和159份归档保持，独立列入历史 |
| 本阶段初次入口 | JSON路径str/Path错误，Popen前失败，0生成 | 原始错误保留；计1次启动尝试，12项修正后驱动检查通过 |
| c02b9414实际服务 | 12输入负例、缺省/null、预算0通过；预算1质量失败 | 4生成，其中预算1正文续写推理并重复close；未替换默认 |
| 31ffc41e修正服务及安装 | 17次新增生成出口全部通过，3次实际服务正常关闭 | 复用不受边界修正影响的12拒绝和缺省/null2次；预算0/1均重验 |

首个真实质量失败揭示单独close与原生answer前缀不一致。修复加入
实际换行/close/空行控制token；没有裁剪最终正文。计量上限由未交付
版本的11更新为14，capabilities、schema、客户端和CPU合同同步。
日志验收也修正为通过HTTP trace→completion ID关联，二者保持独立。

本阶段累计**21生成/12负例/5启动尝试**，包括1次Popen前失败；加上
上一阶段共7启动尝试。完整19个冻结用例由2个有效复用及17个修正后
新增组成；其中包含普通length及严格错误终态。修正包实际执行143.37秒，
首次入口到finalize共1317.76秒，未重置30分钟，也未扩大22/12/5上限。
最终guard COMPLETED，所有失败guard为COMMAND_FAILED，无ENV_ABORT。

## 真实行为证据

| 用例 | 观察结果 |
|---|---|
| 预算0/1 | 均只回答710003；forced3；预算1 consumed1、reasoning_tokens4、completion11，修正前为含重复close的22 tokens |
| 预算16公开JSON/隐藏SSE | 答案及usage一致；consumed16+forced3=19，completion26；实际4个MTP批、14个MTP接受token、fallback1 |
| 预算128自然闭合 | consumed18、forced0；7个MTP批；正确答案与stop |
| thinking关闭且预算0 | consumed/forced均0，正确答案 |
| 严格工具及真实回填 | 预算8，真实read_project_record读取MODEL前8行；canonical reasoning历史回填，回答包含Qwen3.8及精确citation |
| 严格JSON schema | 预算1/隐藏SSE，完整answer=710003对象；不发布部分payload |
| 总输出1 | 普通length、completion1，仅已生成的换行；严格SSE为structured_output_incomplete，无成功finish/usage或可执行payload |
| 私有SSD缓存 | seed prompt8467；2个MTP批/6个接受token后回退；后续prompt8507实际cached_tokens8473，恢复与fresh完整message相等、各completion18 |
| plain | 命令明确--no-mtp；预算0/16均正确，实际MTP步骤为0 |
| 安装路径 | 重新加载build/q4t，预算0正确；completion10、forced3；资源及正常关闭通过 |

缓存只证明该冻结业务的真实恢复和回答一致，不把完整message一致
当作所有张量数值等价。示例端到端响应耗时：预算1为0.753秒，预算16
MTP为1.179秒、plain为1.522秒，缓存恢复1.792秒，安装健康0.788秒。
这些是有界业务观察，不是五档对照或统计性能保证。

31份响应/121个封套通过transport schema；18份成功响应中的99份
message/delta通过reasoning选择/隐藏投影，18份usage通过thinking
投影及动态等式。12个输入负例在流式首帧前400且param精确，token和
worker计数不增加；拒绝路径未改，修正后复用原响应，不伪装为重跑。
每组active chats、严格租约、worker与词表存储归零；服务正常关闭。

## CPU范围与证据

原CPU OFF179/ON188、原生必需145+后端9、客户端27、模板120按依赖
复用。边界修正后新构建零警告，直接重验8项预算、1项真实grammar、
4项capabilities、1项客户端预算和13项驱动检查；不称原全套在新版本
全部重跑。模型/模板/原有输入校验路径未改，5项生产启动预检复用，
每次真实启动前对当次argv及0700 SSD目录再次检查。

原始材料见[证据入口](evidence/serve-thinking-budget-http-20261103/README.md)，
本地根为`.q4t-work/serve-thinking-budget-http-20261103/`。三套包/执行
分别保留原入口、路径修正和边界修正；最终CPU、原失败wire、修正wire、
SDK、trace、metrics、schema、guard及安装收据均封存。共415份直接复制记录、加身份/说明/seal后419文件，约3.91MB；
20个大ELF/bundle等本地产物记录路径与SHA，不重复入Git。

回退在冻结97e2f573源码环境，以新GPU guard运行
`tools/serve_thinking_budget/http_stage.py rollback --boundary --output ABS_DIR`，
保留原绝对证据路径。后续文档/清单变化不能充当冻结源码身份；来源
漂移或未知pair会拒绝。恢复标记处理单文件原子替换间的中断，不承诺
跨文件断电事务。

03:13 CST GPU计算PID空、共享锁可用、无恢复标记/常驻模型，统一RAM
MemAvailable约116.55GiB，未主动丢弃文件页缓存。下一项为SRV-06
文本stop，随后产品预检与有效配置、完整请求协议/JS SDK和工业化运营。
