# SRV-07 请求预检与有效配置交付

2026-11-03 04:49 CST完成有限业务验收与默认安装，04:50资源复核。
长期“持续推进完善serve到工业水准”目标保持active；下一项SRV-08
完整机器输入协议与JS客户端。冻结范围、两次失败后的分组调整见
[开发与验收合同](SERVE_PREFLIGHT_2026-11-03.md)。

## 默认身份与服务合同

- ELF：`5642eb68d0e2d9296aeb0d5f4197797a18d064b2de749c4368a86fce918167ce`。
- sidecar：`6aca6a3ebf951ed8e625ff4944b73c34f40f3854b92497be2a8abe485315e382`。
- 运行时实现b3ae710e，最终冻结驱动24310aba；两次修正只影响验收，
  候选ELF始终相同。旧a3fd64a3/49c7c631与完整回退链保留。
- 本机Thor、完整head、max_len8192、prefill2048；单槽默认MTP k3，
  显式2048/8192双槽plain。双槽MTP只观察启动、配置和一次预检，
  未验证其生成。模型/数值源码未改，不作性能接受声明。

新增私有版本化接口：`POST /v1/chat/preflight`接收完整Chat请求包装，
返回实际模板分词数量、输入字节数、输出预算、prefill块数和合格槽位。
可显式请求模板文本或token IDs；默认不返回这两项。预览以保守JSON
编码上界限制8MiB，超限拒绝，不截断。`GET /v1/config`返回启动后
有效上下文、槽位、MTP/cache、限制和配置来源，不公开模型路径或凭据。
项目Python客户端提供对应方法；这不是vLLM tokenize协议兼容声明。

真实Chat与预检共用输入校验、约束规划、原生模板、Tokenizer及预算。
统一槽位按剩余上下文截输出预算；显式槽位要求输入加已截输出全部
可容纳。max_prefill只是分块大小。模板预览是Tokenizer NFC前字节，
IDs经过生产Tokenizer的规范化；usage包含整个输入。

预检仅文本，即使服务允许实验媒体也拒绝媒体预检。不获取GPU/严格/
缓存租约，不注册取消身份、不访问KV缓存、不编译grammar或执行模型。
受相同body、JSON、分词和文本准入限制；CPU工作仍有成本。不会预留
槽位或保证未来执行成功。grammar实际编译、推理边界token准备、
运行时缓存命中/可用槽位及模型质量明确未检查。

## 实际验证

| 场景 | 观察与结论 |
|---|---|
| 普通JSON/SSE | MTP与plain分别通过；两传输均回答710003，prompt31/completion7；各自message与usage相同 |
| stop、推理预算 | 跨token停止返回ALPHA0123；预算0在原生闭合标记停止，completion3；预算1且隐藏推理的严格schema回答710003 |
| 真实工具闭环 | strict SSE调用read_project_record读取MODEL前8行，回填后回答Qwen3.8-Flash-Next及原始行号引用；每步预检与usage一致 |
| 长工具历史 | 显式tool_choice=none后prompt5385/completion7，回答710003、无工具调用；max_prefill2048下3块预填充 |
| 实际容量选择 | required2048分配slot0/capacity2048；长历史required5513分配slot1/capacity8192，与预检合格槽一致 |
| 精确2049边界 | 只读预检仅slot1合格；没有把required2049作为实际生成请求，不能宣称该点执行验收 |
| 统一槽位边界 | prompt8191、请求INT_MAX时可用输出1；prompt8192拒绝；配置和预算来源与实际argv一致 |
| 错误合同 | 8类Chat输入负例与预检错误相同；包装null/未知/重复字段、路由方法/查询/类型/body及容量拒绝通过 |
| 预检副作用 | 前后请求、prompt/completion、执行步数、worker启动计数不增长；预检指定身份取消返回404；请求owner、strict、worker和词表storage归零 |

这些HTTP服务未开启state cache；不将上表计数观察扩张为真实缓存
命中/恢复验收。无缓存访问还依据共同路径代码与CPU合同。每次真实
生成前的预检数量与最终prompt usage相等；严格schema/工具仍由原有
执行期grammar与最终校验保证，预检不替代执行。

最终schema校验覆盖60份wire、95个封套（77 transport、16 preflight、
2 config）、40个推理message/delta和12份usage。包含首轮合法但业务
质量失败的auto工具响应；协议通过不能把这次失败改写为答案正确。
有效业务11项由7项同SHA复用和4项新plain生成组成。

CPU生产必需host164、grammar后端9、客户端31、模板/实际ID差分125
（113正例/12负例）、最终驱动13通过。差分绑定Transformers5.16.1、
tokenizers0.23.2、Jinja3.1.2；schema绑定jsonschema4.26.0，真实Python
SDK3.42.1。原3组启动参数及新增plain双槽使用真实ParseServerOptions
预检，无模型加载。生产构建零警告，原HTTP生命周期探针也已通过。

普通plain JSON/SSE分别约0.457/0.458秒，长历史4.290秒，小槽位0.582秒。
这是有界业务观察，未运行五档性能对照，不声称性能非劣或性能提升。

## 原始失败与累计预算

1. CPU host OFF198/ON207首轮各1夹具错误：2.0本应是合法整数。
   修正为允许2.0并拒绝非整数，两种构建只重验该项通过。生产测试
   目标遗漏server_options.cpp链接已修复；生产q4t在修正前后SHA相同。
2. 首轮8生成/23预检/15拒绝/1启动，前7业务通过。长历史auto允许工具，
   返回合法first({})、content=null；驱动错误调用strip。记录最终答案
   质量失败，修正业务夹具为显式none，原响应及异常保留。
3. 第二次双槽MTP成功启动；1次预检返回有效数据。驱动继承free=1的
   单槽等待条件，在free=total=2时超时。该次0生成/0拒绝/1预检/1启动。
   改为全部槽位和owner释放；候选未变，新增离线回归检查。
4. 最后1次加载合并plain双槽、长历史和JSON/SSE；4生成/7预检/1拒绝
   全通过。安装后对实际build/q4t检查CLI和grammar worker握手，报告
   post-exec RSS183119872字节；HTTP使用相同ELF的候选证明，没有另起
   安装路径模型。之前的两次服务也都正常退出。

累计**12生成/31预检/16专门负例/3模型启动**，包括失败尝试。
最终业务41.62秒、完整执行42.10秒；首次04:31:46 lease到finalize
1054.62秒，未扩大12/32/20/3与30分钟硬限。两个COMMAND_FAILED保留，
最终guard COMPLETED、ENV_ABORT0，不称首轮全部通过。

## 证据与回退

原始wire、SDK、metrics、配置、schema、CLI/worker、guard、安装记录及
CPU失败见[证据入口](evidence/serve-preflight-20261103/README.md)。本地
`.q4t-work/serve-preflight-20261103/`保留3套包和执行目录；所有原件按
字节/SHA封存，ELF、探针和source.bundle保留本机并登记身份。
直接复制1088份原始记录，归档共1092文件/12.83MB，23个本地大产物
登记身份；归档根seal与Git索引逐字节核对，原包和执行seal也均通过。
首次Git索引仅803份，忽略规则漏收284份wire与5份日志；明确加入这些
已封存文件后1092份全部一致，没有修改原始字节。

回退须使用冻结24310aba源码和原绝对路径，在新的GPU guard中执行
`tools/serve_preflight/stage.py rollback --final --output ABS_DIR`。
来源漂移或未知pair拒绝。两文件各自原子替换，不承诺跨文件断电事务。

04:50:04 CST GPU计算PID空、共享锁可用、无恢复标记或常驻模型，
MemAvailable116.94GiB；未主动丢弃文件页缓存。后续完整请求schema、
JS SDK、受载控制、严格长payload背压、访问运维和长期稳定仍未完成。
