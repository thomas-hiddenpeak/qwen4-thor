# 原生视频文件输入闭环

本项继续A07/A10/A11与Q01—Q03。rev134已发布服务端只处理调用者提供的
video_frames，MP4容器解码在客户端Python示例中。MCP下载的video/mp4
目前进入input_file，通用文档解析不能替代视频准备。该缺口必须实现，
不以字节传输、文档披露或不支持声明结案；默认rev134保持。

## 固定合同与实施顺序

1. 原生有界codec worker：实际识别MP4/WebM容器，以固定软件解码组件
   取得帧、尺寸和原始PTS；采样策略和时长必须明确，不能将采样帧声称
   为完整视频。复用已有独立worker、父死退出、期限/取消、进程回收和
   容量槽，不运行外部Python/ffmpeg程序，不自动切换到硬解码。
2. 统一文件准备：标准input_file与MCP视频共用取得/提取路径，输出
   实际PNG帧、时间戳和来源/codec身份；复用持久快照，重启续接不重新
   下载或解码。进入现有模型video_frames处理，不能用无时序的散图
   冒充视频；补齐Responses工具角色和token计数/生成一致性。
3. 资源与失败边界：源字节、边长、时长、扫描帧/像素、输出帧/字节、
   worker内存/CPU和wall deadline分别有上限，进入跨工具共享预算。
   缺库、畸形、过量、取消和超时只影响该准备请求；未回收worker继续
   占槽，核心readiness和健康模型不能因此停止或重启。
4. 验证与交付：成组冻结针对性CPU，使用本机生成的真实MP4/WebM，
   覆盖颜色/帧顺序/PTS、损坏、缺库、预算、取消、快照续接与业务接线；
   然后做必要的有界SDK/GPU业务、恢复资格及可回退发行。实际云账户
   资料仍独立验收，不把本机文件称为Google/Dropbox真实账户证据。

## 依赖与已有实现

现有GStreamer 1.x系统库与软件插件可承担容器/codec；本机具备
qtdemux、Matroska、OpenH264和VPx插件。新增使用应仅发生在视频worker，
不链接为核心启动依赖，不以随包携带外部程序替代原生实现。代码应
固定允许的库/插件与载入路径，记录实际身份，隔离解析不可信字节。
缺失时返回视频请求错误，普通文字和已准备快照不应受影响。

复用已有FileExpansion、InputSnapshotStore、MCP资源冻结和共享准备
预算，扩展所需的数据合同；现有普通文档/图片和原视频processor的
数值合同保持。原生CPU codec并非Jetson硬解码验收，也不扩大视觉并发。

官方参考：[appsink](https://gstreamer.freedesktop.org/documentation/app/appsink.html)、
[OpenH264解码器](https://gstreamer.freedesktop.org/documentation/openh264/openh264dec.html)。
实现、构建、CPU与GPU状态分别记录；全部master仍active。

## 当前进度

[worker CPU证据](evidence/serve-native-video-worker-cpu-20261116/README.md)：
13项不同C++最终通过，完整引擎零警告，原生固定软件H.264/VP8/VP9已
验证真实颜色与时间。线程累计创建限制复用既有内核通知broker，
不是仅约定线程友好使用；CPU/地址空间/输入输出上限分别生效。
默认与GPU未动，统一输入、快照、共享预算及实际业务尚待完成。
旋转/非方形像素等实际格式组合要保留为后续核对项，不能宣称所有
MP4/WebM变体已经兼容。硬解码和视觉并发仍不扩展。

[统一接线CPU证据](evidence/serve-native-video-input-cpu-20261116/README.md)：
38项不同C++、1项实际CLI策略封包及7项来源负控最终通过，完整引擎
零警告。候选已接通来源/PTS快照、Files与MCP、工具角色、计数、审核、
私有程序结果载体和跨入口零余额；原件删除或缺codec不会触发再获取。
下一步实际SDK/模型业务及恢复发行；旋转/非方形像素组合仍需完成。
