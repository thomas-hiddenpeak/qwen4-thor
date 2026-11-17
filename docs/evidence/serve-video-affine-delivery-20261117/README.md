# 原生仿射与视频细节默认交付 rev244

实际包`ad8f443e7e16adaa864ad1729c4c7cda5fc7338473d89da5e1cb3aadf3e7d976`，generation
`d548cf475ccf93d1ddffb794a8845c8524ffc80889bf7e426eab0e369da28bf0`；引擎`e16c8108ec4bb1b2a3fbcc01641a36657b8b0558a724fe9a019791fd3f262bbc`。
19项STOPPED检查/发布、整根回退与再前进完成，284.680秒，
0新增GPU，outer保持enabled且累计启动0次。

合法仿射显示、v4冻结重放及显式视频detail=high实际可用；每帧
65536像素下限与准入/处理/缓存/历史一致，预算不足拒绝而非静默
降级。auto/low保留checkpoint参考处理。实际JSON/SSE、主轻及
保留控制包见[发行组合](../serve-video-affine-composition-20261117/README.md)。
含原失败累计7启动24请求（含自检及embedding）、323.503秒严格
租约、308项NVML正常，完整清理。原默认小尺寸和含答案Chat题
质量失败单列；新模式/中性题通过不追认旧题，也不声明根因解决。

core/client/outer预算字节保持；profile仅扩展已验视频合同，原历史、
pending、selected、deadline和周期保持。实际回退恢复各根原周期，
只递增revision。人工保留根`f329ce2277dd5d7fdc70e8716bc5068abf97287e40f744dcdf20796aad9121ad`和
自动旧控制payload`9535ff875171e04ea4c1c011ae1b2558e26204805b9ad57280e44afb1d1cdd07`均使用
当前引擎；未声称独立旧引擎回退。控制器/native helper/client未变。

退休generation及登记各两份可恢复归档，0删除，原234包和day入口
保留。继续刷新用户day独立交接、PR41实际合main及全部master。
一般透视、多轨、其余格式和后端仍需实际实现，阶段不是总终点。
