# 原生说话人可回退交付 rev296

默认服务包`4945bfea581033b42c2b88bb2fbd8c053b266426b268104a7a8b55089293773c`，引擎`e5c65f79cd38f46f3d24cd2fefb845f1bbb1c81e37f82808b829b6bced3c71f2`。
19项STOPPED检查/发布、实际整根回退与再前进全部完成，262.464秒，
0新增GPU；outer启用且累计启动0，服务保持STOPPED。

同人、换人回访、已知参考、JSON/SSE、双SDK、原生TLS、主轻及
整包回退后的实际A/B/A已验，原ASR/对齐/VAD与核心业务保护保持。
参见[直接HTTP](../serve-speaker-http-20261118/README.md)、
[主轻](../serve-speaker-profiles-20261118/README.md)和
[原生服务/恢复](../serve-speaker-service-20261118/README.md)。

核心/client/outer预算字节保持，profile仅显式增加已验speaker身份
及预算；原history/pending/selected/deadline保持，实际整根回退恢复
各根原版本周期。人工根`157d3c0b53b86ce4f27c49533de2fd335f7ffd32c5f213a1008671e5da3afae4`、自动payload
`cad13869f52f2d5616577750c3fd1913304640345c0e9f132a833acc83f05f54`均使用当前引擎与保留控制；未宣称独立
旧引擎具备新增能力。前驱rev286及原day入口保留。

4 intent/4 generation/3 version可恢复归档，0删除；原5项plan
被现有4项保护拒绝的失败保留，修正私有脚本后完成。资产/辅助
故障只降级对应能力，真正设备故障保护保留。已验托管配置使用
8个声音窗口，CLI默认256；预算均显式绑定，不将容量拒绝算成功。

下一步完成主线检查和合入；重叠语音实际检测分离、Chat音频/
合成/Realtime和其余全部master继续。没有宣称完整OpenAI兼容、
全模型质量、性能或长稳通过，总目标仍active。
