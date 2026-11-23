# Realtime 旁路的独立连接名额

同客户先保持一个主 WebSocket 打开，原生入口与可选 nginx 均对第一个合法 `GET /v1/realtime?call_id=rtc_cpu` 返回 429。模型侧已使用独立旁路名额，两个入口把它计入了主连接。[修复前的两个实际入口](original.txt)

原生入口现与模型使用同一路径识别函数。nginx 的精确原始查询匹配先于通用 Realtime 规则，核对 call ID 长度、字符和查询形式。已占用主连接时，两种实际入口均允许同一客户再保持两个旁路，第三个旁路收到 429。无效、重复或混入模型选择的 call ID 查询无法取得旁路名额。逐连接确认了 1000 关闭帧。Live 旁路及普通 HTTP 的既有容量检查也通过。[四项进程回环](fixed.txt) · [来源审计](inventory.json)

验证在 Thor 上使用真实原生入口、nginx 和独立 CPU WebSocket 对端。媒体与模型业务复用[此前的真实验收](../serve-realtime-webrtc-create-20261123/README.md)。
