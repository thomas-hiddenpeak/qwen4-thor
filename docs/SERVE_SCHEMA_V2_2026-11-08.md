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
worker回收；该机制目前只是明确的实施方案，尚未编写或验收。
