# SRV-02h+02i组合GPU验收事前合同

2026-11-03，父39cb0ba3，业务可靠性任务。生产280b95dc与validation
00718dea保持，已通过[接线CPU](SERVE_MEDIA_INTEGRATION_CPU_2026-11-03.md)
按来源复用，不重跑全组。当前新vLLM仍占GPU，保留服务；本批先开发
驱动、夹具及其CPU检查，实际GPU执行须全程同一独占lease。

## 冻结范围

沿用02h未执行的9个场景、17次生成/8负场景：生产普通JSON/SSE、严格
JSON和真实Node SDK项目资料工具回填；实际红图/两相同帧、坏图；视觉
output OOM同PID恢复、copy/host-after-forward/free错误停服、host早期/
SSE固定错误后恢复、显式视觉启动失败拒绝。坏图预期按新合同改为
media_format_unsupported，不更改实际传输字节。

增加两个启动场景：

1. validation媒体准入，11个固定负例：非规范base64、MIME不符、detail
   未支持值、源边长、帧尺寸不一致、累计RGB、累计patch、展开prefill、
   展开context、实际attention上限、decoder临时分配配额。
   每例携带同一限定request_id，在vision.pixels_allocate预先武装OOM。
   所有负例均应在该点之前返回，逐个检查code/param/完整wire和所有者
   回收；随后合法图必须触发该OOM，证明故障仍未被提前消耗，再用另一
   请求恢复红图答案，核对同一PID和计数。prefill与quota两例使用SSE，
   其它负例JSON。共13生成/12负场景，不以C++单测代替真实HTTP。
2. validation媒体取消：仅指定请求在既有vision.after_forward检查点
   最多暂停10秒，取消API返回202后立即释放门，要求原SSE一个取消错误
   和一个DONE、无成功终态；随后同PID红图恢复。共2生成/1负场景。
   这是视觉完成附近的协作取消，不声称抢占单个C解码/kernel。

累计计划32生成尝试、21负场景、11启动；硬限35/24/12、384控制请求、
2400秒（含finalize，90秒清理预留）。备用额度不授权自动重试/扩样。
负场景包括预期400/503/SSE错误、取消和启动拒绝，全部如实记账。
发现实际失败立即保留记录，后续只在具体修复后冻结受影响补验。

原生25 HTTP生命周期、tiny视觉权重33 OOM/3 host/1缺失/1成功、workspace
20 OOM/4 copy及333真实视觉权重上传继续保留。新增小型设备探针直接
链接相同生产vision目标，核对实际函数shared属性与准入拒绝，并注入
查询失败/未知属性确保输出归零；不修改/重编译生产运行时代码。

媒体+seq-capacities仍由现有CLI拒绝；本合同不扩该组合，也不把假想
显式槽位的host测试称为上线支持。prefill/context使用合法单槽媒体
配置，实际函数能力同时与config/capabilities对账。

## 夹具和取证

PNG头部负例由固定CRC/尺寸生成，尺寸无效者应在decode之前拒绝。
解码配额夹具宣称1×1 RGB，但IDAT展开为256MiB+1零字节；使用固定
1MiB块流式压缩生成，生成器不分配整个展开缓冲。实际processor必须
在256MiB所有权限制前拒绝，记录逻辑峰值并紧接合法图恢复。先运行
一次针对该新夹具的CPU检查；不制造物理OOM、不扩大RAM额度。

负例完整读到EOF后再解析JSON/SSE，不因收到error就提前结束；复用
SDK的失败续写判定但不伪造成功。准备阶段封存候选、旧默认pair、SDK/
模型只读输入、全部工具来源与夹具；旧02h包和CPU包保持原seal。
无旧包launch/刷新hash绕过；失败不自动发布、成功仍须全项交付门禁。
默认替换/sidecar/有界安装健康/回退包/阶段提交沿用既有授权。

本项不证明物理RAM硬限额、所有媒体质量、真实公网/多人、任意压缩
数据、长稳或性能非劣；这些仍在工业化验收账中独立保留。
