# 原生视频显示方向与像素比例

2026-11-16，A10/A05针对性正确性与业务改动；已交付rev150，非性能优化。
下文保留各阶段身份与失败，当前验收/交付以末段及STATUS为准。
此前worker使用编码尺寸直接转PNG，未完整应用显示矩阵和非方形像素，
会使模型看到旋转或拉伸的画面。本项实现这些能力，不以拒绝合法变体收口。

## 实现与边界

在既有隔离worker内固定加载系统good包的videofilter。实际安装的
qtdemux遗漏反射矩阵的方向标签，因此原生有界MP4读取器检查atom长度、
版本、节点数和单个视频track，组合movie/track的八种正交显示方向；
videoflip执行该方向，WebM继续源方向标签模式。复用已有
videoconvertscale，按实际解码CAPS
核对正整数尺寸与像素比例。只扩展需要校正的轴、最邻近整数取整，
在转换分配前检查边长/像素预算；每帧累计预算按校正尺寸计入。
最终PNG实际尺寸须与计算结果或其旋转一致，不能继续信任编码尺寸。

转换记录v3保存编码尺寸、像素比例与显示变换策略；快照仍离线验证
PNG哈希、尺寸、帧时间/时长和来源。v1/v2旧快照保持读取，无codec
也可续接。新版本不得丢失v2已有的实际帧显示时长。

固定插件来自已经使用的GStreamer good系统包，不新增外部服务程序，
不链接为核心启动依赖；缺库/变换失败仍只影响对应视频准备请求。
根据[官方videoflip API](https://gstreamer.freedesktop.org/documentation/videofilter/videoflip.html)、
[方向枚举](https://gstreamer.freedesktop.org/documentation/video/gstvideo.html)
与[缩放API](https://gstreamer.freedesktop.org/documentation/videoconvertscale/videoscale.html)
核对，2026-11-16查阅；未复制插件代码，继续保留系统LGPL许可说明。

## 验证与交付

原创四象限视频及独立预期角点颜色覆盖90/180/270度、四种镜像，
MP4/WebM宽像素、窄像素与旋转组合；另测放大预算/溢出、转换记录
篡改、v1/v2/v3冻结回放。首轮13项有2项失败；已定位反射方向丢失
和EOS覆盖容量错误，修复后8项受影响用例通过，6项未变成功记录复用。
共14项不同C++最终通过，2项业务驱动CPU通过，714来源锚点/7项负控
通过；主机/引擎零警告。原失败完整保留于
[CPU证据](evidence/serve-video-geometry-cpu-20261116/README.md)。

候选`6135c33e`接下来最多1模型启动/4个创建/600秒：上传的90度旋转
MP4、inline镜像SSE、像素比例与旋转组合及同PID普通文本。只向模型
提供原始视频和问题，角点颜色/布局预期留在独立判定器；核对计数与
实际usage、自有资源清理。复用未改数值/恢复证据，不跑完整性能或
质量矩阵。其他仿射变换、格式与组合仍属于A10未完工作。

本阶段不扩展硬解码或视觉并发，其他格式与组合仍在master推进。
PR29已按精确head独立通过CI并实际合main（692982e7），本批未混入
其交付；实际默认rev142保持STOPPED。显示修复CPU通过，业务和发行
尚未验收，全部master继续。

## 首次业务结果与定点补齐

6135c33e首次实际业务1启动/2创建/43.682秒，旋转与计数通过；镜像
角点正确但layout答square，未验收。原始失败见
[业务记录](evidence/serve-video-geometry-business-failure-20261116/README.md)。
实际冻结帧128×64，新增CPU检查实际ProcessVideo网格仍为128×64，
没有发现像素被方形化。通用视频来源投影补充验证后的displayed_frame
宽、高，使精确几何可供应用回答；不加入layout分类/颜色答案。
[5项针对性CPU](evidence/serve-video-display-source-cpu-20261116/README.md)
通过，主机/引擎零警告。新候选a7bd6785保持原问题和独立预期，补验
最多1启动/4创建/600秒；当前累计1启动/2创建，实际默认仍rev142。

## 当前验收结果及交付

a7bd6785[实际业务通过](evidence/serve-video-geometry-business-20261116/README.md)：
1启动/4创建/52.610秒，51项NVML正常；三段画面的角点和布局、计数、
JSON/SSE及同PID普通文本全部通过。当前累计2启动/6创建，原失败保留。

新二进制须建立精确主/轻配置资格，不改写旧包记录。冻结最多2启动/
10请求/900秒，每侧一个结构化任务、文档工具与续答、原生embedding
和视频任务；复用已有驱动及未改控制/数值证据。资格通过后组装可回退
发行；rev142在此之前保持，不把本项验收代替其余总计划任务。

主轻资格已实际通过（2启动/10请求/87.791秒，83项NVML正常）；
正式8d9c07f1、保留a9bd9c73通过两项sealed namespace CPU。
[证据与复用审查](evidence/serve-video-geometry-profile-business-20261116/README.md)
确认控制源码和四个原生程序不变。接下来保留包1启动/2生成/300秒，
正式包整树版本回退2启动/4生成/1探测/600秒，验证镜像原视频和
冻结SSE续答；不是重新做RAM压力、性能或完整正确性矩阵。

[精确整包验证](evidence/serve-video-geometry-version-business-20261116/README.md)
也已通过：保留包1启动/2生成，正式包2启动/4生成/1探测；冻结镜像
SSE继续正确，能力/配置历史和停止/清理闭合。下一步17项STOPPED
发布检查，rev142→保留144→正式146→回退148→正式150，0新增GPU；
真实Key/配额/core/outer及profile历史/冷却保持，版本周期保留前驱。


## 默认 rev150 已交付

[17项STOPPED交付检查](evidence/serve-video-geometry-delivery-20261116/README.md)
通过，54.855秒、0新增GPU；保留144→正式146→实际回退148→前进150
全部完成。当前8d9c07f1包/a7bd6785引擎，旧默认与状态备份保留；Key、
配额、core/outer与profile历史/预算/冷却保持，版本新周期保留前驱。
接下来PR30精确head检查后实际合main，随后继续全部master缺口。
