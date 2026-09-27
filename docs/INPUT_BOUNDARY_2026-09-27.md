# JSON与HTTP输入边界修复

2026-09-27：有界缺陷回归完成，默认部署仍为b5f38e48。本节点继承
[decode错误终态修复](DECODE_FAILURE_2026-09-27.md)，不改变模型kernel。

## 修复与兼容合同

审计中约200KB嵌套JSON使实际解析器在独立进程中SIGSEGV；非法数字/
控制字符和冲突HTTP长度头也被接受。现于分配/递归发生前执行边界检查。

| 对象 | 当前合同 |
|---|---|
| JSON递归 | 最多64层对象/数组；第65层在进入递归前拒绝 |
| 请求JSON节点 | 最多65536个值，包含对象键；超限400 |
| 请求JSON字符串 | 累计解码字节最多16MiB，包含键；超限400 |
| JSON语法 | 严格数字、小数/指数必需数字；拒绝不在double可表示有限范围的数值，包括下溢；拒绝未转义控制字符、非法UTF-8及孤立surrogate |
| 重复字段 | 请求体和JSON文本形式的tool arguments拒绝重复键，转义后同名也拒绝 |
| 整数转换 | GetInt/AsInt对非有限、小数或double值超出int64范围返回默认值，禁止越界强转；数字原始拼写按原开关保留 |
| 生成字段 | 根必须object，stream必须boolean；max_tokens必须为[1,2147483647]整数，之后保留原max_len夹限 |
| HTTP头 | 最多64KiB，完整字段名匹配；请求行须为HTTP/1.0或1.1 origin-form；HTTP/1.1须有唯一非空Host |
| HTTP正文 | Content-Length严格十进制、最多16MiB；拒绝重复长度头，即使数值相同 |
| HTTP未支持模式 | Transfer-Encoding和Expect显式拒绝；一个连接一个响应后关闭，不支持pipeline |

模型文件解析也有64层上限，默认节点4Mi、累计字符串256MiB；保持模型
文件重复键的既有first-match查找行为，不与外部请求合同混淆。
本模型tokenizer和实际模型加载已验证。受支持数值不是任意精度JSON
数值；超范围返回错误，不静默生成inf或做未定义整数转换。

HTTP解析从chat_server.cpp提取为独立
[http_request.cpp](../src/server/http_request.cpp)，便于使用同一生产
函数作socketpair测试。原来全文搜索`content-length:`会误认
X-Content-Length；现在它只是未知字段，不承担正文分帧。只给该字段
的生成请求因没有合法JSON正文而400。畸形HTTP由HandleClient返回400。

这些是有意的错误输入兼容变化：过去接受/夹成1的max_tokens=0、负数、
小数、错误类型现在拒绝；超过64KiB的请求头、缺少Host的HTTP/1.1、
重复字段、未支持的传输编码也不再被宽松接受。并非完整OpenAI参数
schema；temperature/top_p/stop等参数的支持/拒绝表仍属下一节点。

## 验证身份与结果

候选SHA256：
`2efb5b6712f7642d42e2a637961316340a8ce2879febd5da8d69ebaf028dfc18`。
实际CMake构建来自`.q4t-work/sequence-slot-build-20260927/`；候选及
实际CMakeCache位于`.q4t-work/input-boundary-final-20260927/`。
主机检查位于`.q4t-work/input-boundary-20260927/`。

| 检查 | 结果 |
|---|---|
| JSON/HTTP主机测试 | 12项通过，ASan+UBSan+float-cast-overflow，无告警；含100000层JSON、节点/字符串预算、Unicode、长度冲突、64KiB头及16MiB正文边界 |
| 本模型tokenizer | 4项通过，无skip；加载、golden编码、往返及特殊token |
| 模板字节对照 | 21/21同冻结fixture |
| 真实HTTP非法请求 | 32类均400，每类之后健康200/GPU健康/槽位1/1；前后正常1K答案、usage、stop同参考，服务退出0 |
| 固定11题evalscope HTTP | 11/11全文同参考，含200K；原始SSE/usage/stop复核通过，服务退出0 |
| 真实Nginx代理七组 | 7/7通过，含200K、过载/慢上传/断连/取消/恢复；4条正常SSE复核，Runner/Nginx均退出0 |
| 五档性能接受 | 本节点未运行，未替换正式性能参考或默认部署 |

HTTP覆盖深度、节点、非法数值/Unicode、重复键、根类型、stream类型、
max_tokens范围/类型、取消入口、二次工具参数解析，以及错误长度头、
TE/CL、超大正文声明、超大头和Expect。驱动
[run_input_boundary.py](../tools/evalscope/run_input_boundary.py)保存请求
原始字节、响应及健康状态，不向用户模型目录写文件。

初版候选通过30类HTTP后，审查发现JSON文本形式工具参数使用二次
解析，随后共享请求预算并新增2类HTTP检查；最终32类在新目录完整
通过。初版目录`input-boundary-direct-20260927`保留，最终证据为
`.q4t-work/e2e/input-boundary-direct-v2-20260927/`，两者身份不混用。
父候选f410c3c6的4组decode故障与8组取消证据仍按原身份保留，不能
写成对2efb5b67重新做过同一矩阵。

179个文件在全部服务和驱动退出后完成封存，候选目录
`artifact-binding.json`的SHA256为：
`b174e53a7968d4b26506603aa3d1ad7782a1230e7cf5b627d92f4498c8f1b584`。
封存包括实际CMakeCache、源码快照/摘要、主机测试/构建日志、请求/
响应/健康、evalscope数据库、代理配置及退出记录。格式调整后重构建
仍为同一2efb5b67二进制。质量/代理驱动位于候选目录，均退出0。

## 未覆盖与下一步

本节点不解决媒体解压前预算、全局并发主机内存上限、连续慢上传的
总时限、全部API参数schema和测试框架skip计PASS，也不保证任意输入
组合/系统资源状态不OOM。HTTP parser是明确受限实现，不宣称完整
HTTP规范兼容或已进行协议模糊测试。

默认build/q4t与正式性能参考保留原身份，完整升级接受仍需五档E2E。
下一节点冻结首版文本greedy范围、明确不支持参数的拒绝合同、建立
必跑且不能因skip/零匹配而误绿的发布入口，然后进行最终性能/部署
验收。已知输入缺陷修复和业务语料质量评估继续分开记录。
