# 统一schema、输出预算与真实场景回归结果

2026-11-07，按[冻结计划](VISION_CLIENT_PLAN_2026-11-07.md)完成限定交付。
验收实现`a4cd5b40fe18e60b2bc11f973980529e0e5f5232`，主线入口
[PR #17](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/17)已在两项检查
通过后合main 5ba744da，合并树等于6999936f；本阶段重复分支清理及
保留锚点见[合入记录](evidence/vision-client-20261107/mainline.json)。
[机器结果](evidence/vision-client-20261107/result.json)、
[固定问题/真值/来源](evidence/vision-client-20261107/corpus.json)及
[原件绑定](evidence/vision-client-20261107/bindings.json)随仓库保留。

## 可以使用的入口

共享helper将同一object schema送入response_format并追加到最后一条user
消息，支持文本字符串、媒体parts与已有真实工具结果后的最终回答。
字段与枚举的描述可帮助模型理解语义，约束后端负责已支持子集的语法；
**两者都不能证明事实正确**。已有response_format、活动工具与缓存组合
会被拒绝，不修改调用方对象、工具结果或模型答案。

```python
from tools.examples.chat_client import make_request, with_json_schema

# 普通文本或媒体：messages沿用已有字符串/parts合同。
request = make_request(messages, [], json_schema=schema, max_tokens=768)

# 已完成真实工具读取后，应用明确决定进入最终回答。
final = make_request(tool_history, tools, max_tokens=768)
final["tool_choice"] = "none"
final = with_json_schema(final, schema)
```

```javascript
import { makeRequest, withJsonSchema } from './tools/examples/js/chat_client.mjs';

const request = makeRequest(messages, { max_completion_tokens: 768 },
  { jsonSchema: schema });
const final = withJsonSchema(makeRequest(toolHistory,
  { tools, tool_choice: 'none', max_tokens: 768 }), schema);
```

JS支持任一预算字段，拒绝同时提供max_tokens和max_completion_tokens；
原来显式max_tokens与默认max_completion_tokens冲突的问题一并修复。
Python公共默认512、JS默认256保持。schema为有效UTF-8、最多64KiB、
有限可互通JSON数值；服务preflight仍检查完整schema子集和资源限制。

```bash
python3 -m tools.examples.vision_query --image /path/document.png \
  --question '请按schema提取字段。' --schema-file /path/schema.json \
  --max-tokens 768

python3 tools/examples/project_query.py --base-url http://127.0.0.1:8080 \
  --directory .q4t-work/new-query --question '读取项目当前状态并给出引用。' \
  --max-tokens 768
```

视觉CLI仍默认256，资料CLI仍默认512；参数为1～INT32_MAX的整数，实际
可用预算受上下文/准入限制。视觉示例现在拒绝finish_reason=length，
保留已有协议错误；不自动增加预算或重试。资料自动工具循环本轮只增加
预算配置，结构化最终回合由应用通过共享helper显式构造。
旧视觉helper保留转发入口，紧凑JSON改变了提示空白，因此本轮重新验证
正反视频保护，没有直接把旧提示证据套用到新请求上。

## 固定业务结果

| 场景 | 入口与结果 |
|---|---|
| 项目设备实拍：机舱、2扇可见窗、灯亮、无人 | Python JSON与JS SSE各通过 |
| 自然地球照片：地球、可见云、没有路牌 | Python SSE与JS JSON各通过 |
| 两图顺序：第一张机舱、第二张地球 | Python SSE通过 |
| 真实记录柱图：phase-05b为13，最高phase-04 | JS JSON通过 |
| 原合成视频正向/反向，schema明确枚举含义 | Python/JS SSE各通过 |
| 纯文本结构化17+25 | Python JSON、JS SSE均42 |
| 真实只读工具与最终schema回答 | 1次实际读取，Python/JS最终回答四值、对照和引用全对 |
| 输出预算刻意设为1 | HTTP502 structured_output_incomplete，未发布成功 |

随后使用同一真实工具历史，将预算显式设为768，两个客户端均完整结束；
实际各输出57token，无工具重放。它验证预算传递、错误处理与显式续答，
不是“任意复杂回答768都足够”的结论。上轮256耗尽/289token自然回答是
另一个请求合同，其原失败和证据保留。

实拍来自只读reference，原图7749×5812超过16MP源限制，准备阶段仅全幅
Lanczos缩放到960×720；原图、变换和像素结果均绑定。自然照片沿用ds4
fixture，其README记载NASA Apollo17来源。真值在推理前固定；没有生成
物体或用模型答案反推真值。图表是实际验收数据的程序渲染，仍不是扫描。
两张照片、一个记录和两段原合成视频组成小型固定集，不代表真实扫描、
任意视频或复杂场景的整体准确率。单次客户端耗时实拍约11.54秒、自然
照片3.50～3.60秒、多图11.96秒；没有性能对照或SLO接受。

## 验证与CI收尾

- CPU：首组89 Python/43 Node通过，之后预算防负值和审计负控5项通过
  （2项复验），累计92项不同Python。公共host新增Python schema合同，
  workflow新增独立Node schema合同；旧客户端/工具恢复未被跳过。
- HTTP：1模型启动、14Chat、17控制、0原生，107.814秒完整独占租约；
  13个完整成功响应（其中1工具调用）与1个预期低预算错误。没有追加
  第二模型或补满剩余预算。
- 独立审核原始JSON/SSE、HTTP错误、展开preflight/usage、真实文件行号与
  引用、两客户端实际请求、图像变换及视频几何真值；6457文件绑定、
  2193 Git来源核对，241运行来源与默认ea1b474d/sidecar3eb46385不变。
- guard COMPLETED，100次监控无冲突/不可读；自有PID4037353消失，最终
  NVML空、18146无监听、原锁inode3294067可获得并释放。custody ack仍
  是注册后观察，另核对waitable/reap和退出后监控；轮询不能排除短暂干扰。
- 旧文档f9ff50b3的CI第2次在同提交通过。第1次取消因依赖下载约9.5分钟，
  进入build后触及10分钟总时限，未完成测试，取消原件不重写。新workflow
  只补缺失依赖，复用已有编译器/CMake，固定numpy对应系统Python，保留
  全部检查；依赖6分钟、总任务15分钟。实现提交的push/PR两项检查通过。

原件位于`.q4t-work/vision-client-20261107/originals/`，按SHA寻址，完整
映射在同目录父级originals.json。复核可在项目根执行：

```bash
python3 -B -c 'from tools.vision_service.client_audit import audit; print(audit()["status"])'
```

再次推理回归必须先冻结新的阶段目录/预算，保留本次原件，不能覆盖原目录
或把后续不同模型/提示自动记为本次通过。默认程序、数值合同和旧回退包
不变；旧托管/任务/Key/day包继续固定其旧来源，不迁移。全局
media_acceptance=false、experimental=true保持，硬解码、视觉并发和长稳
未扩展，长期稳定验证仍由用户开展。
