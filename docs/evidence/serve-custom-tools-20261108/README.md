# Custom工具实现与可回退交付证据

2026-11-08；源码`bf4191a1`。实际默认引擎f8677aaf、协议33b3d128，
服务包6be35d37。264份原件逐字节保留，SHA/来源见[index](index.json)。

47 C++、72输入/schema、17 Python客户端、5驱动和9 JS通过。
`cpu-05`夹具漏字段及生成来源摘要失败保留，`cpu-06`定向修复；
build-01/02早期告警/类型错误及CPU计划准备错误也保留。最终生产
build-06与夹具build-07零警告，未改client/input证据依赖明确复用。

首两轮分别1启动/5Chat和1启动/1Chat，混合工具任务失败；原结果、
source commit与ef1aeb97副本回执保留。第三轮改变模型适配为原生
function/input格式，保持标准custom.input及编码语义，以原用户
混合任务通过。最终1启动/8Chat/63.730秒，两种实际工具读取、
text/regex/Lark、标题引用、截断/取消和同PID MTP通过。
累计3启动/14Chat/168.634秒，严格租约172.004秒、163次NVML全OK。

619来源及旧失败来源Git字节核对，44控制面运行文件和原生二元组
不变。安装0模型/0Chat，原ledger/Key/策略/配额保留STOPPED；
旧33c52e4a/13f077b6回退在delivery-01/rollback，GPU/端口/锁释放。
上游原始许可证随不可变发布元数据和发行licenses目录保留；未
引入额外运行服务。模型/ELF/私有策略不进Git。A04完成不代表全部
OpenAI兼容或总目标完成，继续A05和其余计划；尚未合入main。
