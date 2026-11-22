# Live WebRTC 前端权限和原生媒体格式

2026-11-23。启动配置解析器在发布会话前检查前端数据通道的权限。省略和 `all` 允许适用事件，空名单禁止全部。入站按事件类型匹配；出站的 `response.event` 还须精确匹配嵌套 Responses 事件。每份名单最多 256 项，服务端选择器名称最多 256 个 Unicode 字符。配置失败保留先前的权限。[官方创建合同](https://developers.openai.com/api/reference/resources/live/methods/create)。

原生模型规划器允许媒体会话省略公开音频格式，内部使用媒体子进程输出的 24 kHz 单声道 PCM16，继续拒绝由媒体客户端指定线上格式。WebSocket 的三种显式格式继承既有数值合同。

[Thor CPU 记录](cpu.json)包含 3 项新增验证、9 项启动配置和 1 项原有音频映射回归。覆盖同一外层 Responses 事件不同内层类型、空名单、256/257 项和 Unicode 边界、格式隔离及失败后原配置保持。模型运行时构建通过，本组无 GPU 启动。HTTP 媒体会话创建和真实前端权限执行仍待接入。
