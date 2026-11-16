import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];W=Path(__file__).resolve().parent
result=json.loads((W/'delivery-01/result.json').read_text());current=result['publication']['current']
assert result['status']=='PASS_CHAT_STYLE_STREAM_DELIVERED' and result['publication']['revision']==158
p=ROOT/'docs/STATUS.md';s=p.read_text();start=s.index('## 当前实际默认');end=s.index('八云提供方',start)
s=s[:start]+f'''## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev158；服务包 `{current['manifest_sha256']}` |
| Generation | `{current['generation_sha256']}` |
| 引擎 | `{current['engine_binary_sha256']}` |
| 引擎发布记录 | `{current['engine_release_sha256']}` |
| Guardian | `383bcc63b26fe0911d1d99b455c1b4effc39b6f813e147a9cfbfb6bce5bf11dc` |
| 原生入口 | `8c669d808962d74612c1ae2e6cee7758d2c19bfba6bf1c938a582d69aa91585a` |
| 服务状态 | `STOPPED`；原生外层监护已配置enabled，累计启动0次 |
| 默认容量 | 单流、ctx8192、prefill2048、输出256、MTP k=3；inflight8/queue2；媒体已启用 |
| 版本回退 | 已验旧控制/当前引擎包 `1b90a1bf135bcca17dbbe4fdf86ea33937de059db354b717b3936b1e8b8c48d7`；保留媒体/MCP/embedding及新Chat能力 |
| 外部依赖 | 默认原生入口及自监护不要求nginx/systemd；视频codec按请求在隔离worker加载；数据源/凭据与TLS/CORS按部署配置 |

[Chat样式与流式合同交付](evidence/serve-chat-style-delivery-20261116/README.md)
完成17项STOPPED发布/实际整根回退再前进检查，{result['seconds']:.3f}秒、0新增GPU。
完整effort/null/medium缺省、verbosity真实指令、SSE默认obfuscation及
关闭选项已发行。none的模板/grammar边界统一；真实文档单次工具调用/
SHA续答、JSON/SSE、多choice概率与usage通过。首轮失败保留并修复。
精确主轻资格、保留包及正式包版本恢复有独立实际证据。

core/client/outer字节、Key/配额和容量保持；profile迁移只增加已审查
的effort集合规则，历史/预算/冷却不清空；版本新周期保留前驱。
四个原生辅助程序和其余控制源未改，旧压力/外层监护证据按身份复用。
最终STOPPED、GPU释放；rev150默认/状态私有备份和可恢复归档保留。
自动恢复保留现代根，人工整根回退v12入口无outer，仍是R09待解决项；
保留包不是独立旧引擎回退。PR31待最终head检查与实际合main。
前驱rev150显示几何已通过PR30合main（`d2524e4a`），
[合并回执](evidence/serve-pr30-merged-20261116/README.md)保留。

''' +s[end:]
mark='## 最近完成的实现和验证\n\n';s=s.replace(mark,mark+'''[rev158可回退交付完成](evidence/serve-chat-style-delivery-20261116/README.md)：
17项检查通过，默认cba9f596/e0b34b43，停止意图/历史预算/Key保持。
先前真实工具失败、迁移拒绝和两处路径调用缺陷均保留原记录；
修复后按受影响项验收。继续PR31及标准输入/媒体缓存/后端/资源等
全部master任务；本组完成不代表完整兼容或总目标结束。
以下为按原身份保留的阶段记录，“待发行”不代表当前默认状态。

''',1);p.write_text(s)
p=ROOT/'docs/log/2026-11-16.md';s=p.read_text();head='# 开发日志（2026-11-16）\n\n';p.write_text(head+f'''## Chat 样式与流式合同 rev158 可回退发行

- 日期：2026-11-16。正式cba9f596/e0b34b43完成17项STOPPED交付，
  {result['seconds']:.3f}秒、0新增GPU，实际整根回退再前进通过；最终STOPPED，
  outer enabled且累计0启动。core/client/outer字节和容量/Key/配额保持。
- 完整effort/null/medium、verbosity指令、默认SSE padding/关闭选项
  与none grammar修复进入默认。新主轻及精确包版本恢复已验；一处
  迁移控制源支持已审查枚举扩展，自动等价/禁止丢能力保护保持。
- 原失败、rev150备份及两份无活动引用generation可恢复归档保留；
  旧控制/当前引擎1b90a1bf不是独立旧引擎回退。人工旧根无outer仍待补。
- 为什么：交付必须同时具备真实功能、状态连续及可核验回退，不能只关PR。
- 下一步：PR31最终head检查及实际合main，继续标准输入合同和全部master。
- 证据：[交付记录](../evidence/serve-chat-style-delivery-20261116/README.md)。

'''+s[len(head):])
p=ROOT/'README.md';s=p.read_text();needle='[工具选择与增量流](docs/SERVE_TOOL_STREAM_2026-11-08.md)支持';assert needle in s
s=s.replace(needle,'''[Chat样式与流式合同](docs/SERVE_CHAT_CONTRACT_PARITY_2026-11-16.md)已交付：
`reasoning_effort`支持none/minimal/low/medium/high/xhigh/max/null，缺省/null
为medium；none关闭原生thinking。`verbosity`的low/medium/high/null映射
实际提示，不承诺固定答案长度或计算量。流式delta默认带obfuscation，
可用`stream_options.include_obfuscation: false`关闭；padding不计token，
正文、工具参数及usage由客户端正常重组。Responses的缺省推理级别尚待
本轮后续合同修复，具体默认/回退身份以STATUS为准。
''' +needle,1);p.write_text(s)
p=ROOT/'docs/ARCHITECTURE.md';s=p.read_text();old='''  接入和调度；默认数字IPv4 127.0.0.1:8000。只实现规定范围的OpenAI
  风格接口，当前生成采用greedy，不支持完整OpenAI参数语义。''';assert old in s
s=s.replace(old,'''  接入和调度；默认数字IPv4 127.0.0.1:8000。Chat/Completions/Responses
  已有真实生成、采样、多choice、logprobs、工具和结构化输出；Chat默认
  temperature=1，符合条件的显式greedy保留MTP。标准字段/后端和组合
  仍按Serve总计划补齐，不以这些已交付子集宣称完整兼容。''')
s=s.replace('当前冻结矩阵是纯文本单流k=3。','当前托管包为单槽媒体及文本k=3，\n  有视觉token的请求使用普通解码；并发扩展仍须对应证据。')
p.write_text(s)
p=ROOT/'docs/SERVE_CHAT_CONTRACT_PARITY_2026-11-16.md';s=p.read_text();s=s.replace('本项是兼容性与业务行为修复，不是性能优化；候选尚未验收或发行，\n实际默认仍为 rev150/a7bd6785。','本项是兼容性与业务行为修复，不是性能优化；已通过针对性CPU、\n真实业务及可回退发行，实际默认为 rev158/cba9f596/e0b34b43。')
s+='''
## 实际验收与交付

[CPU](evidence/serve-chat-style-cpu-20261116/README.md)、
[真实首轮失败](evidence/serve-chat-style-business-failure-20261116/README.md)
及[共享thinking边界修复](evidence/serve-chat-style-grammar-cpu-20261116/README.md)
分别保留。修复后[原六条业务](evidence/serve-chat-style-business-20261116/README.md)
通过，包括一次真实文档工具/SHA续答；
[主轻资格](evidence/serve-chat-style-profile-business-20261116/README.md)和
[精确版本组合](evidence/serve-chat-style-version-business-20261116/README.md)通过。
发布准备发现旧迁移规则拒绝effort枚举扩展，
[针对性CPU修复](evidence/serve-chat-style-migration-cpu-20261116/README.md)
保持自动恢复等价/禁止丢能力，新正式包再做
[受影响版本短验](evidence/serve-chat-style-migration-business-20261116/README.md)。
[rev158发行](evidence/serve-chat-style-delivery-20261116/README.md)完成17项
STOPPED交付/回退/前进检查，状态预算保持；不追加全模型或性能矩阵。

Responses默认medium、其余七个标准Chat字段、历史audio/annotations、
媒体状态缓存/后端及其他全部master继续，阶段发行不是总任务结束。
''';p.write_text(s)
p=ROOT/'docs/serve-master-goals-v2.json';v=json.loads(p.read_text());v['updated']='2026-11-16'
for t in v['tasks']:
 if 'chat_style_parity_20261116' in t:
  t['chat_style_parity_20261116'].update(state='delivered_main_pending',default_unchanged=False,revision=158,manifest_sha256=current['manifest_sha256'],delivery_evidence='evidence/serve-chat-style-delivery-20261116/README.md',profile_evidence='evidence/serve-chat-style-profile-business-20261116/README.md',version_evidence='evidence/serve-chat-style-migration-business-20261116/README.md')
 if t['id']=='A01':
  t['remaining_required']=['Chat seven frozen-reference top-level fields: audio/modalities/moderation/prompt_cache_options/safety_identifier/service_tier/web_search_options','Remaining default/null, nested message and response projection parity beyond delivered Chat style fields']
 if t['id']=='A02':
  t['remaining_required']=[x for x in t['remaining_required'] if x!='Complete Chat effort enum/null mapping using native semantics']
 if t['id']=='Q02':
  t['release']=dict(binary_sha256=current['engine_binary_sha256'],manifest_sha256=current['manifest_sha256'],record='evidence/serve-chat-style-delivery-20261116/delivery-01/result.json')
  t['remaining_required']=['PR31 exact-head checks and actual main merge','Delivery of every remaining implementation']
  t['pr30_actual_merge']=dict(commit='d2524e4a71114c58d9282d9e75a3a0252f5e6192',evidence='evidence/serve-pr30-merged-20261116/README.md')
v['next_task']=v['next']='Complete PR31 exact-head main integration; continue standard input contracts (Chat attribution/service tier and Responses medium default), then remaining media/cache, backend/tool, client and cold/resource/host compositions. All 33 required master tasks remain the completion boundary.'
assert len(v['tasks'])==33 and all(t['required'] for t in v['tasks']);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
p=ROOT/'tools/serve_audit/inventory.json';v=json.loads(p.read_text())
for name,old,new in [('reasoning_effort','候选实现，业务/发行待验。','实际业务/可回退rev158发行已验。'),('verbosity','候选尚未发行，其他标准字段仍须实现。','rev158已发行，其他标准字段仍须实现。'),('stream_options','候选待CPU和业务验收。','CPU、真实SDK业务和rev158发行通过。')]:
 assert old in v['fields'][name]['boundary'];v['fields'][name]['boundary']=v['fields'][name]['boundary'].replace(old,new)
 v['fields'][name]['evidence'].append('docs/evidence/serve-chat-style-delivery-20261116/README.md')
v['chat_style_delivery_20261116']=dict(status='delivered',revision=158,engine_sha256=current['engine_binary_sha256'],package_sha256=current['manifest_sha256'],evidence='docs/evidence/serve-chat-style-delivery-20261116/README.md',master_active=True)
p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
print('Updated seven current documentation/ledger files; historical log entries retained')
