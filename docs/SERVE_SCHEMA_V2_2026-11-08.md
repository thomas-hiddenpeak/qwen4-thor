# A05：标准结构化输出与组合能力

A04已交付f8677aaf。继续总计划A05，下面是同一任务的实现顺序，
不是删除剩余能力或停止总目标的理由。依据为2026-11-08抓取的
[官方结构化输出指南](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas)，
本机原文保存在`.q4t-work/serve-schema-v2-20261108/reference`。

## 确定缺口

- 本地v1拒绝`$defs/$ref`、递归、`anyOf`、数值enum/const与数值/
  pattern/format；规模仅8层/128节点/32枚举，低于官方范围。
- Qwen原始XML参数不能区分字符串`null`与JSON null，属性名/值
  包含闭合标签时也不能直接无损使用；须补模型适配，不能删能力。
- 固定XGrammar的number multipleOf会被忽略，integer在部分范围
  组合也忽略multipleOf；float范围固定6位生成精度。仅放宽本地
  allowlist不能保证约束，必须补自己的精确适配与最终校验。
- JSON response_format与工具、结构化与session/prefix cache当前
  被拒绝，媒体组合与结构化SSE也需真实核对，不能只改发现接口。

## 实施与验收

1. 共享schema图与十进制精确值：本地JSON Pointer、递归与anyOf、
   enum/const、声明次序、required/null，明确资源上限和错误路径。
   数字比较/整除不能经binary64四舍五入或容差接受错误输出。
2. 有界生成语言与独立值校验：完整官方关键字，pattern/format与
   数值约束的组合不被忽略。复用现有隔离worker和静态regex解析器，
   无新增外部运行服务；展开过大属于资源失败，不冒充已实现通过。
3. 工具模型适配与协议组合：复杂/nullable参数保持标准arguments，
   JSON与工具按实际输出分支处理；session/cache、媒体与取消不丢
   状态或放松约束；SSE逐片可见但成功终态及EOF仍是使用屏障。
4. 最终编译零警告，按受影响schema/语言正负例、解析/SDK/缓存与
   工作器边界冻结CPU清单。真实业务普通组≤2启动/8Chat/900秒，
   媒体独立组另列具体清单；不跑完整性能或全模型正确性。
5. 真实通过后可回退交付并继续A06等全部总计划。当前仅开发，
   不把未接入的校验/编译器帮助函数当成对外能力已经完成。

官方基准范围包括10层、总5000属性、总1000枚举及相关字符串规模；
保留有界编译、进程资源与输出预算。标准子集之外的扩展不默默降级，
但官方范围内的缺口不能以unsupported或文档说明结案。

## 基础模块开发进展

schema图与精确十进制帮助模块已编写，尚未接入HTTP/生成路径。
覆盖本地引用/递归/anyOf、精确enum/const、数值范围和整除、Unicode
长度与标准规模；pattern/format必须提供实际checker，缺失时明确
失败，不会跳过。现有生产v1保持，下一步生成语言及字符串checker。

`build-01/02`零警告；9项针对性CPU检查最终通过（含1717组独立整数
余数对照）。首轮8/9通过，$ref错误路径夹具未使用既有转义格式，
修正并统一根引用错误路径后仅重验该项通过。原件保留在
`.q4t-work/serve-schema-v2-20261108/cpu-01/02`，未运行GPU。

有界正则交集与精确数值生成帮助模块已完成7项CPU检查：Unicode
断言/长度交集、独立匹配与工作预算、十进制范围/multipleOf、超过
2^53的整数、243个独立整数余数对照及空区间。首轮数值组合触发
资源限额，第一次子集乘积修复仍有Unicode边重复；两次失败保留。
最终改为可达状态集合乘积、同目标区间合并、无断言图不区分类别，
没有提高8Mi工作量/4096节点限额；受影响7项全部通过。
`build-03`—`build-07`零警告，原件在`cpu-03/04/05`。累计16项不同
帮助模块检查通过，仍未接入HTTP/默认程序；下一步完整schema生成
及格式checker，然后接入请求、工具、媒体和缓存组合。

## 组合编译与字符串最终校验开发

组合schema编译帮助模块已支持保持引用身份的递归、anyOf与同级约束
交集、精确enum/const筛选、多multipleOf精确LCM、声明顺序对象和
可选字段、数组范围；5000属性实际EBNF编译/匹配通过。生成字符串
经统一JSON编码器保护引号、控制字符及工具闭合标签。`cpu-06`的
6项新编译用例与6项受影响图校验全部通过；尚未接入HTTP。

原生最终值checker已实现9种格式和pattern共同生效。引入MIT许可的
[JSON-Schema-Test-Suite固定数据](../tests/fixtures/schema_formats/README.md)
（245a45abc90190c0bff04d97e6d2b541b4bb9f12）；439个外部字符串用例
最终通过。首轮hostname有24项失败，揭示Punycode/IDNA2008上下文
规则缺口；修正末尾点规则并复用本机libidn2的register接口后全部
通过。38项本机IDNA定向诊断与首次失败均保留，修复后只重验hostname、
email与直接受影响的组合/依赖错误。库不可用、分配失败、非法编码
保持不同结果，不把依赖失败变成schema无解或非法用户值。

日期/时区/闰秒校验按RFC3339；本地已知闰秒表依据2026-11-08读取的
[IERS Bulletin C 72数据](https://hpiers.obspm.fr/iers/bul/bulc/Leap_Second.dat)，
有效期至2027-06-28。无请求时网络查询。其余格式依据
[JSON Schema格式规范](https://json-schema.org/draft/2020-12/json-schema-validation#section-7.3)
与[RFC5321 Mailbox](https://www.rfc-editor.org/rfc/rfc5321.html#section-4.1.2)。
原始web摘录保存于reference/formats-web-excerpts.txt；直接抓取首个
规范收到HTTP403，使用浏览工具摘录，未冒充完整原站下载。

`build-08`—`build-11`零警告，`cpu-06/07/08`保留原件。累计35项不同
CPU帮助模块检查最终通过；0 GPU，默认f8677aaf仍保持。

### 尚需完成才能对外验收

- 字符串生成语言必须同时满足pattern/format/length，尤其时间的
  时区与闰秒、hostname的IDNA上下文；不能用宽泛正则替代这些规则。
- libidn2仅是开发帮助模块按需调用的系统库（本机2.3.7），不是新
  服务程序，也尚未进入当前发行。实际接入必须留在有界隔离worker，
  包含最终值检查、加载失败/恢复和错误传播；不能把新库加载放进
  健康模型的关键路径。发行前登记依赖、许可和可用性信息。
- 完成约束模式下精确数字的JSON读入/输出容量合同，不能因保留数字
  原文就忽略现有binary64解析边界。
- 统一HTTP/schema/模型工具参数适配和真正的结构化增量SSE；完成
  JSON与工具、会话/prefix cache和媒体组合，再做已冻结业务短验。

上述均保留为A05必做事项；帮助模块CPU通过不代表A05交付或总目标完成。

公开host构建也已完成（XGrammar/CUDA关闭），3项直接相关native格式/
依赖错误检查通过，复用其余身份未改的CPU证据。首次配置缺OpenSSL
头文件路径而失败，复用此前已接受的本机头文件后构建零警告；未
安装系统软件，失败保留在public-build-01。最终build-12零警告。

下一步的生成保证采用现有语法mask加隔离worker内的增量语义检查：
字符串完成前检查候选token对应的格式值，拒绝不合法的闭合；引用/
anyOf保持仍可能满足的分支，完成后再做独立完整值校验。IDNA加载与
最终checker均留在worker的时间/内存边界内，依赖错误保留独立协议
状态。需验证候选检查预算、UTF-8/转义跨片、多个值/分支、取消与
worker回收；增量parser/图校验帮助模块已开始实现，token mask与
worker实际接入尚未完成。

增量JSON prefix与schema图的前缀检查已完成8项针对性CPU：每个字节
截断的UTF-8/代理对/转义、严格语法/重复键、协议后缀消费边界、
字符串闭合时格式检查、anyOf存活分支、精确数字完成时检查、递归/
const/长度及资源/依赖错误保留。build-13零警告，cpu-09全部通过；
累计43项不同CPU帮助模块通过。该帮助模块仍未接入实际token mask，
不能据此声称对外生成已具有新增格式保证；0 GPU，默认不变。

## 隔离worker接入与真实词表发现

新增字符串语法与schema token guard已接入开发版grammar worker，尚未
从HTTP planner启用。worker对完成值独立检查并输出绑定schema/工具
作用域/精确值的摘要回执；父进程接收有界回执，解析器消费仍待完成。
系统IDNA检查限定worker，独立dependency错误可在编译阶段返回503，
未修改模型健康/重启决策。JSON reader新增显式精确数字模式，普通
模型/配置解析默认仍拒绝binary64越界；HTTP入口尚未启用新模式。

引用深度4项、格式语法与精确数字7项、语义guard及实际隔离worker
7项通过（cpu-10/11/12，累计57项不同A05用例）；439个外部字符串
同时用于语法+独立checker核对。build-14至18零警告，0模型/0 GPU。

真实248k词表和1024字节字符串检查cpu-13在第4个token触发2秒
单步超时。mask-diagnostic-01在20秒CPU上限内定位：语义过滤约
1.2ms，词法mask随字符串长度从0.65秒升至4.91秒，未完成即终止。
首个仅整理JSON编码的修复仍在同位置超时（cpu-14：其余15项
受影响检查通过）。原件保留，不增大生产超时。继续修复生成语言
的普通字符递归表示，并针对失败及受影响语言检查重验。

HTTP/schema/工具/缓存/媒体组合和真实业务验收继续必做；当前默认
f8677aaf及服务包没有替换，A05和总目标均未完成。

实际修复已通过：将正则状态自循环直接输出为字符类重复，并在JSON
编码中保留普通Unicode的连续字符类，避免每个字符追加匹配栈。
build-19/20/21零警告；cpu-15的15项受影响语言/格式/实际worker
检查通过，真实248077词条、150个token、1024字节字符串及闰秒
值完成，仍保持2秒单步限额。首次仅JSON编码的失败不删除；这里只
接受业务缺陷修复，不据CPU结果宣称HTTP性能优化或整模型验收。

cpu-16的6项worker协议检查通过：schema绑定不匹配拒绝、host专用
故障注入分别传播dependency/resource，以及新请求正常恢复；复验
既有取消回收、内存上限、thinking和forced token事务。注入代码仅
在host测试ELF，不在生产worker；这不是移除本机系统库的实测。
累计60项不同A05用例通过，另复用/重验受影响旧合同；下一步父进程
消费回执、HTTP/schema规划及模型工具参数适配，0模型/0 GPU。

生产q4t_server静态目标和公开host（XGrammar/CUDA关闭）也构建零
警告：build-22/public-build-03。公开host定向4项JSON/引用/prefix
检查通过（cpu-public-02），复用其余已过且未改的证据。新worker
依赖/语法代码进入开发构建但未进入默认包；未启动模型进程。

## HTTP规划、工具JSON适配和回执终态开发接入

2026-11-08：开发版已使用schema v2图校验请求，复杂工具通过单一
原生arguments参数保持原始JSON对象，模型定义中的本地引用正确
重定位；历史支持任意JSON键、null/字符串、精确十进制及标记转义。
响应按字节增量解析，父进程只比较隔离worker的schema/工具名/值
回执，不加载IDNA库。JSON response_format可与工具同时声明，
根据输出选择JSON正文或工具序列；JSON正文与推理也逐片发送，
截断、回执缺失/错值/错作用域/额外回执均无成功终态。

Chat/preflight精确读取schema数字，生成参数仍拒绝非有限值；修复
超大数组长度上限进入整数转换的风险，并保持传统参数历史的有限
十进制原文。空闭合对象、引用数组和精确错误路径已覆盖。

build-23/24及26/28零警告。cpu-17为7/7，cpu-18为9/10（旧用例
固定片段数量），cpu-19为26/27（旧用例要求缓冲正文）；断言分别
改为实际重组内容/终态并定向通过。cpu-20的5项和prefix-01的2项
全部通过。新增真实planner→worker→parser及回执检查5项、精确
历史1项，累计70不同A05 CPU通过；旧检查只重验直接受影响项。
build-25发现独立prefix校验目标遗漏现有模块链接，改为链接真实
q4t_server后恢复；build-27的测试名称空间遗漏修正后build-28通过。
所有首次失败保留，不新增GPU/模型启动，不替换默认f8677aaf。

公开请求schema、能力声明、客户端与缓存/媒体组合仍在继续同步，
GPU真实业务和交付未完成；本开发提交不表示A05或总目标完成。

## 公开请求schema、能力发现与资源错误同步

2026-11-08：公开request/message/vision/OpenAPI合同已同步本地引用、
anyOf、精确数值、format/pattern及JSON/工具组合；任意JSON历史键不再
作为函数名拒绝。128工具声明实际进入worker，超出官方上限拒绝。
能力发现报告v2真实上限和增量输出；schema资源预算错误、运行中
worker依赖/资源/超时保持独立code及503，失败终态不带成功usage。

build-29零警告，包含生产探针及原独立HTTP生命周期目标；补齐其
custom/schema链接依赖。独立CPU探针probe-build-01也零警告通过。
input-01精确冻结49项公开schema/生产规划对照全部通过；cpu-21
五项通过，新增2项使累计不同A05 C++ CPU用例为72。
生成文档第一次check发现clang-format后来源hash变化，保留旧语义
文件与失败记录并重新生成；请求形状未变，不重复49项验证。

默认f8677aaf未替换，0模型/0 GPU。示例客户端仍有旧组合与数值
限制，继续实现客户端、缓存和媒体组合及真实HTTP验收；不将公开
shape通过冒充生成/业务交付，A05与总目标保持进行中。

## 客户端精确数字与组合开发接入

2026-11-08：Python/JS示例与视觉离线构造入口移除旧JSON+工具、
根引用、必须user和64KiB schema限制，与服务v2的2MiB预算一致。
实际工具结果与消息顺序保留；缓存组合仍待下面的状态布局实现后
同步开放。Python通过标准库Decimal，Node通过ExactJsonNumber或
BigInt表示超出常规浮点范围的数据。传输保持JSON数值token，不转
字符串、不先舍入；普通控制字段/usage仍按其原有限数值合同读取。

JS精确数字序列化使用JSON.rawJSON，当前固定Node24.18.0具备该
原生能力；缺少时显式报错，不悄悄损失精度。没有新增库/服务依赖。
Python极端指数超出Decimal容量时保持ProtocolError。UTF-8非法
字节和输入字节上限在Node离线入口拒绝。客户端终态/EOF后才执行
工具的规则不变，允许schema与工具并存不等于立即执行半个调用。

client-01：16 Python、6 JS schema、16实际固定SDK/传输检查通过；
client-cli-01的精确数字与坏UTF-8两项通过；client-02新增极端
指数错误及受影响精确编码两项通过。累计本组17不同Python、22 JS
和2 CLI。记录实际发送字节及响应重组；无网络/模型/GPU。
继续目标缓存状态布局、媒体组合及真实业务验收；默认f8677aaf
不变，A05尚未交付，总目标保持进行中。

## 结构化输出的文本状态缓存开发接入

2026-11-08：目标模型专属快照进入实际生成路径，保存attention、
GDN/PLE递归状态及有效RoPE。恢复校验精确token前缀和布局；
MTP与target布局不能串用，私有会话仍共用同一客户端隔离租约。
请求语法、sampler和worker回执每轮重新建立；至少保留一个未缓存
输入token重算logits。共享种子和普通解码会话保存继续受成功HTTP
终态发布约束，失败/取消不发布。no-MTP文本配置可实际启用缓存，
无须加载草稿；原MTP布局保持原先字节顺序。

公开合同、能力发现与示例客户端同步开放schema+文本缓存；legacy
函数与JSON的组合也移除公开shape中的过时拒绝。媒体schema组合
仍须真实短验；媒体状态缓存尚未接入，不把文本快照当作MRoPE及
图像身份实现，现有媒体配置仍明确拒绝启用状态缓存。此未实现组合
留在A10多模态组合任务，不作“已解决”或删除。

构建build-30完成实际q4t/host/probe，零警告；同次make未识别新
测试目标，保留失败。显式CMake生成后build-31新目标零警告完成。
cpu-cache-01：4个新CPU状态/真实SSD检查、8个受影响host合同、
旧存储49+共享39断言、2 Python与2 JS通过。input-cache-01的21项
公开shape/生产探针对照通过。public-build-04零警告，关闭CUDA与
XGrammar的7项直接受影响检查通过（cpu-public-03）。
累计77不同A05 C++ CPU用例；本阶段尚无模型/GPU执行，默认
f8677aaf与服务包未改。继续真实工具/schema/cache短验和媒体短验，
通过后可回退交付，并继续A06及总计划。

## 实际SDK桥与文本业务短验准备

2026-11-08：补齐Python→Node→固定SDK桥的精确数值传输，避免
通用桥在SDK前舍入整数或拒绝Decimal。实际子进程/SDK和内存
Fetch检查5项通过（client-bridge-01），覆盖精确数字、真实资料
执行记录、JSON/SSE、HTTP错误与缺失DONE不提交。无网络/模型/GPU。

`tools/serve_schema_v2/acceptance.py`冻结1次模型启动、8条Chat、
900秒、零自动重试：丰富schema JSON/SSE与精确整数、共享种子
及私有会话恢复、实际资料读取工具与JSON最终回答、截断不发布
缓存、随后同PID普通MTP。两个后续请求只由本轮真实结果生成。
既有生命周期/租约/清理实现复用；driver-cpu-01三项通过，含实际
生产planner准备和错误终态/舍入/错误缓存证据负控。
尚未运行GPU；此计划不包含完整性能或全模型正确性矩阵，媒体
独立组随后冻结。当前默认与保留视觉候选不改，总目标继续。


## 文本业务失败与有限长度语法修复

2026-11-08：business-01先导实际1模型/5Chat、102.843秒。前四条
丰富schema JSON/SSE、精确整数、共享1024与私有8192以上target
缓存、真实资料工具读取及增量参数通过。第五条最终JSON失败503
constraint_step_timeout；其原请求与真实工具结果完整保存。模型
退出0、无残留身份、端口18164/18165释放、严格租约连续；此失败
不是ENV_ABORT，也未发布默认。首次漏传inherit-lease在构造器即
拒绝，0启动/0Chat；launch-correction.json保留后使用正确租约传递。

真实词表CPU复现有限长度标题超时：词法mask随字符增加从约0.66
升至2.44秒，语义约1毫秒。转换保留字符计数重复，且移除正则交集
永远无法到达终态的路径；Start/End断言只需边界分类，word/line
仍保留各自字符上下文。长度、pattern、Unicode与JSON转义合同及
2秒worker限额保持。最初只处理无pattern长度的修复可通过，但
pattern组合仍失败，现由统一正则层修好，不留下该同类缺陷。

build-32及cpu-title-01—06保留全过程：01漏messages的夹具先行
拒绝、02实际超时、20秒diagnostic、03错误调用无checker父层验证的
断言、04局部通过、05组合失败、06最终18项全部通过、构建零警告。
43个长度/Unicode/转义边界、非连续长度集合、无效前缀、实际248077
词表的纯长度和pattern组合及受影响原数值/格式用例覆盖；累计81
不同A05 C++。此前无约束字符串实际词表通过且无被裁剪状态，复用。

repair.py仅重放原失败final请求（原工具结果、题目、schema均不改），
再执行原未运行的truncated/recovered/greedy；原四条业务证据复用。
其中cache/解析/sampler/模型计算来源不变，正则语言等价性由受影响
CPU覆盖；媒体工具组还会用最终ELF覆盖实际调用。修复组冻结1启动/
4Chat/700秒，累计2启动/9Chat/900秒；多出的第九条只来自这次明确
失败，不扩样或自动重试。最终资料标题须精确对应实际首行（可去
Markdown标题符），不能仅含模型名称便通过。媒体独立短验仍待做，
A05与总目标均未完成，默认及视觉候选未改。


## 完整标题补验暴露二次展开，最终字符上限通过CPU

2026-11-08：business-02实际1启动/1Chat、60.559秒，原final请求
仍触发constraint_step_timeout；退出0、无残留、端口与租约释放。
先前较短CPU标题不充分，未将局部通过外推交付。cpu-title-07改用
完整资料首行后复现；20秒分段诊断仍为词法递归增长。根因是JSON
编码后的(普通字符|转义)重复又被依赖库normalizer展开，首轮字符
链合并只推迟了超时。

custom_grammar现在在normalizer前保留有界终端字符链，使用已固定
XGrammar的公开Regex宏编译等价JSON编码FSM；Unicode/转义一个
标量一次计数，含<安全转义。schema import不再把Regex操作符当规则
重命名。不修改外部源码、不新增依赖或放宽超时。cpu-title-08的
非BMP语法差异(Regex须使用\u{...})及09的宏重命名失败均保留；
10完整标题通过，11最终21项受影响检查通过、构建零警告。除完整
标题外，真实248077词表覆盖256个标量含中文/emoji/引号/反斜杠/
控制字符；原43个长度边界、格式数据及schema/custom导入也通过。
累计82不同A05 C++；此前Rust语言检查实现未再改，复用cpu-title-06。

repair第二次来源表漏列驱动CPU测试文件的准备拒绝0/0也保留在
repair-prepare-01。第三组冻结原final及三项从未执行的请求，1启动/
4Chat/700秒；累计上限3启动/10Chat/900秒（此前已花163.402秒，
2启动/6Chat）。两条超时都保留，不增加成功场景样本或自动重试。
默认与视觉候选未改，A05媒体业务及交付继续，全部总目标仍active。
