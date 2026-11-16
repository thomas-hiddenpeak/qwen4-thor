"""Apply reviewed current descriptions, leaving dated evidence immutable."""
import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
W = Path(__file__).resolve().parent
P = ROOT / 'tools/serve_audit/inventory.json'
j = json.loads(P.read_text())
before = copy.deepcopy(j)
REPORT = 'docs/SERVE_SURFACE_REAUDIT_2026-11-16.md'
E = 'docs/evidence/'

def update(category, key, semantics=None, boundary=None, evidence=()):
    row = j[category][key]
    if semantics is not None:
        row['semantics'] = semantics
    if boundary is not None:
        row['boundary'] = boundary
    for path in (*evidence, REPORT):
        if path not in row['evidence']:
            row['evidence'].append(path)

def field(key, semantics=None, boundary=None, evidence=()):
    update('fields', key, semantics, boundary, evidence)

field('model', '服务主名称或显式注册别名；Chat省略使用配置默认，未知标识404。',
      'Models列表/详情提供稳定创建时间；别名不选择另一组生成权重。专用embedding使用自己的注册标识。',
      (E+'serve-completions-20261108/README.md', 'src/server/model_catalog.cpp', 'src/server/chat_plan.cpp'))
field('messages', semantics=before['fields']['messages']['semantics'].replace('qwen3.8-openai-v3', 'qwen3.8-openai-v4'), boundary='当前托管包启用单槽媒体，直接serve显式启用。原文件、HTTP(S)/inline图像、MP4/WebM、真实显示尺寸和冻结快照通过共享准备/模板/计数；普通文本保留MTP。普通Chat媒体限user，内部Responses工具媒体另有认证路径。audio/annotations历史和完整媒体缓存仍是A02/A10待实现项。',
      evidence=(E+'serve-managed-vision-business-20261116/README.md', E+'serve-video-geometry-business-20261116/README.md'))
field('stream', boundary='JSON/工具/推理增量是暂定输出，成功终态前完成语义校验；首角色、空闲心跳、usage和错误终态保持。实际结构化增量与SDK已交付；不得在失败或截断终态执行工具。', evidence=(E+'serve-schema-v2-20261108/index.json',))
field('stream_options', boundary='Chat目前仅include_usage bool；include_obfuscation在Completions/Responses已有实现，Chat仍须接通A01，不能以拒绝视为兼容。')
field('request_timeout_ms', boundary='生成请求截止从完整body之后计算，不包含读体。grammar独立exec worker可取消/终止，100ms清理后未回收仍占槽；不宣称任意GPU工作可硬抢占。', evidence=(E+'serve-grammar-reap-20261108/README.md',))
field('tools', boundary='非strict schema作为提示；strict使用已交付schema-v2与最终校验。约束编译/matcher在单个exec worker内、2GiB RLIMIT_AS、10s/2s预算；未回收不增殖，失能不停止健康普通文本。媒体状态缓存组合仍待A05/A10。', evidence=(E+'serve-schema-v2-20261108/index.json',))
field('reasoning_effort', boundary='Chat原生模板目前仅low/medium/xhigh，尚未接通Responses已有none/minimal/high/max等映射；不是独立计算预算。其余标准枚举/null须A01/A02补齐。')
field('add_vision_id', '原生媒体占位编号选项；嵌套chat_template_kwargs优先。', '纯文本无效果；直接serve显式启用媒体，当前托管包已启用单槽媒体。')
field('response_format', '已交付schema-v2：local refs/anyOf/精确十进制/pattern/formats；JSON正文或合法tool-only分支，worker最终语义回执在成功终态前。',
      '10层/5000属性/1000枚举/120000受约束字符和独立worker资源上限。工具、媒体与文本缓存有实际验收；媒体状态缓存和stop组合尚待实现，不称全JSON Schema方言或整机RAM硬上限。',
      (E+'serve-schema-v2-20261108/index.json',))
cache_boundary = '显式启用，按认证客户端命名空间隔离；缓存不是持久会话。原生独立worker隔离文件I/O，失能按miss/save失败继续，取消清理有界且不重启模型。文本及结构化组合已交付；媒体身份/MRoPE/恢复仍待A05/A10。'
for name in ('session_id', 'prefix_cache_scope'):
    field(name, boundary=cache_boundary, evidence=(E+'serve-cache-route-delivery-20261112/README.md',))
field('prefix_cache_write_tokens', boundary='正值需要prefix_cache_scope；仍受槽位和缓存布局限制。文件I/O及取消已隔离，不能由此推断媒体状态缓存可用。', evidence=(E+'serve-cache-route-delivery-20261112/README.md',))
for name in ('tool_choice', 'parallel_tool_calls'):
    field(name, boundary='parsed messages与约束后端按实际组合要求使用；named恰好一次，parallel=false最多一次。单constrained租约忙时503；编译可取消/终止，100ms清理后未回收占槽；资源保护不等于全机RAM硬上限。', evidence=(E+'serve-schema-v2-20261108/index.json',))
field('prediction', boundary='实际目标验证、accepted/rejected计量及取消已有针对性业务；仍按prediction自身采样/choice/工具组合合同校验，不承诺请求的预测一定被接受。')
for name in ('functions','function_call'):
    field(name, boundary='legacy单调用规范化为共享工具/schema-v2计划，保留function_call响应与role=function历史；与非null现代控制歧义组合拒绝。媒体状态缓存等剩余A05组合继续实现。', evidence=(E+'serve-schema-v2-20261108/index.json',))
for name in ('store', 'metadata'):
    field(name, boundary='已交付客户端隔离的Chat CRUD/筛选/分页与过期治理；成功终态前获得持久回执，未知写回执不自动重放。store默认false；metadata按16条/键64/值512字符校验。', evidence=(E+'serve-retention-delivery-20261110/README.md',))
for name in ('prompt_cache_key','prompt_cache_retention'):
    field(name, boundary='A07.6文本路径已实际验收交付；有界SSD匿名快照，缓存故障回退正常推理。实际token/布局相等才命中，不是远端托管缓存等价实现；媒体指纹和MRoPE仍待A10。', evidence=(E+'serve-prompt-cache-delivery-20261110/README.md', E+'serve-cache-route-delivery-20261112/README.md'))

completion = {
 'best_of': ('1..128候选，缺省等于n；按真实生成token平均logprob选择n个结果。', 'best_of>=n；stream时不能隐藏更多候选；所有候选消耗计入usage/额度，批次总候选<=128。'),
 'echo': ('bool/null，默认false；回显原始Unicode文本或真实解码token输入。', 'FIM标记/后缀及空文档内部seed不回显；echo+logprobs包含实际提示token概率。'),
 'logprobs': ('整数0..5/null，返回真实原始模型token概率及文本偏移。', 'null关闭；首个echo token无可用前驱概率时为null，原字节与所选token边界保留。'),
 'max_tokens': ('整数0..2147483647/null，缺省/null=16；0可用于echo/评分。', '输入+输出需满足槽位容量；按所有候选计算额度，不静默裁剪请求。'),
 'model': ('必填主名称或注册别名；响应保留所选标识。', '未知模型404；别名不改变权重。'),
 'n': ('1..128整数/null，默认1；每个prompt返回n个独立choice。', 'best_of>=n，所有prompt乘best_of<=128，真实生成串行且共享请求准入。'),
 'prompt': ('原始字符串、token ID数组或同类型批量；省略/null/空文档均有原生语义。', 'token必须在实际词表内；批量不混字符串和token数组，空文档seed不伪造echo正文。'),
 'stream': ('bool/null，默认false；原生text_completion JSON或SSE。', '多prompt/choice事件索引稳定，未选候选不能流出；错误/取消不会发布伪成功终态。'),
 'stream_options': ('include_usage/include_obfuscation均为bool；需要stream=true。', 'include_usage按实际总候选计量；obfuscation默认启用，随机填充不属于正文/概率。'),
 'suffix': ('字符串/null；非null走实际fim_prefix/fim_suffix/fim_middle输入。', '必须能由实际词表精确编码专用token；后缀不拼接到生成结果冒充FIM。'),
}
for name in j['completion_fields']:
    if name in completion:
        semantics, boundary = completion[name]
    else:
        semantics = j['fields'][name]['semantics']
        boundary = j['fields'][name]['boundary']
    update('completion_fields', name, semantics, boundary,
           (E+'serve-completions-20261108/README.md', 'src/server/completion_plan.cpp'))

groups = {
 'stored': ('客户端隔离的已存Chat CRUD、metadata筛选、cursor分页与原消息读取。', '实际持久回执、重启/过期治理已交付；store=false不产生持久Chat对象，未知写回执不重放。', 'serve-retention-delivery-20261110'),
 'conversations': ('原生Conversations会话及items CRUD/分页，认证owner与Responses关联。', '实际模型续接、工具历史、重启和过期治理已交付；历史对象读取不等于可绕过当前模型/容量/权限。', 'serve-retention-delivery-20261110'),
 'prompts': ('原生客户端私有Prompt目录、不可变版本和条件更新，供Responses引用及变量准备。', 'Prompt管理是本地扩展；原生TLS/SDK/实际模型引用已交付，绑定原owner和版本；不能覆盖历史版本或借变量提升权限。', 'serve-prompts-delivery-20261110'),
 'response_state': ('持久Responses读取/删除、input_items、后台原事件续读和取消。', '后台拥有任务/准入、取消与过期治理已交付；查询/取消独立额度，未知提交不重放实际执行；剩余输入/工具后端不由状态API代替。', 'serve-retention-delivery-20261110'),
 'responses': ('原生Responses JSON/SSE、reasoning/summary/encryption、状态、原生工具执行；namespace/custom/async/discovery/programmatic/MCP已有交付。', '实际准备/工具/输出共享父预算、权限与终态；云提供方真实账户、其他内建工具/模型后端及完整SDK矩阵继续A07/A10/A11/A12。', 'serve-mcp-delivery-20261116'),
 'compact': ('真实模型记忆步骤与认证压缩窗口，返回可回填CompactedResponse。', '共享原请求准入、截止和真实用量；已交付实际模型/SDK验证，不把压缩当作无损token截取或跳过权限检查。', 'serve-compaction-delivery-20261109'),
 'count': ('标准input_tokens形状，计数实际初始模板；新表格可执行有界准备，原文件/冻结历史共用准备。', '计数不是最终答案或Response发布；准备另用固定usage响应头，实际模型步骤依输入而定。实际count-vs-usage已验；不是托管模型token等价保证。', 'serve-prepared-source-business-20261116'),
 'files': ('原生Files multipart持久上传、metadata/list/delete和定长二进制下载。', '客户端/存储/读写租约隔离，显式文件与body限额；原生TLS/SDK/实际工具消费和兼容发行已交付，格式处理能力按A10继续。', 'serve-files-business-20261110'),
 'uploads': ('原生Uploads创建/分块/顺序校验完成/取消，原子发布File。', '未知持久回执保留原ID、不重复写；真实SDK/工具消费与可回退发行已过，文件大小/存储/客户端权限仍执行。', 'serve-uploads-delivery-20261110'),
 'batches': ('原生持久Batch创建/读取/分页/取消、恢复队列和逐项原始结果；在线/批处理分别计量。', 'Chat/Completions/Responses、Moderations和Embeddings实际执行已交付。八种合法端点名称中images/generations、images/edits和videos尚缺真实后端；A09/A10继续，不按名字接受计通过。', 'serve-batch-delivery-20261111'),
 'moderations': ('原生独立Moderations：实际受约束分类步骤，文本/数组/图像、累计额度与Batch接入。', '实际模型业务与发行已验；分类结果不是人工安全审查等价保证，依赖失效返回对应错误且不停止健康Chat。', 'serve-moderations-delivery-20261111'),
 'embeddings': ('原生Embeddings文本/token输入、float/base64与维度校验；提供真实生成模型LAST向量及独立Qwen3-Embedding-0.6B检索后端。', '专用权重显式配置且不随代码分发，缺失只降级对应能力；真实参考向量、检索和Batch已验，不用通用LAST向量的早期失败代表专用后端。', 'serve-native-embedding-delivery-20261111'),
 'vectors': ('原生owner隔离vector store CRUD、文件成员/content和持久file_batches、真实文档索引。', '实际Files/TLS/索引及Responses file_search引用已验收交付；语料/embedding身份/权限先于模型工作，取消保留原持久任务语义。', 'serve-retrieval-delivery-20261112'),
 'search': ('原生检索用真实主模型改写、专用embedding、完整语料和共享请求额度，返回来源绑定结果。', '权限先于执行，计全部query TPM及单次外部RPM；快照冲突不返回部分结果/重放模型。实际API及file_search业务和发行已验；不是任意语料质量保证。', 'serve-retrieval-delivery-20261112'),
 'tunnel': ('原生MCP reverse tunnel元数据、单次handoff poll和原token响应；自有agent连接私有部署目标。', '原caller/agent权限、instance affinity、独立poll/response准入和deadline；实际SDK/MCP业务及恢复发行已交付。八云适配CPU已验，真实账户权限/内容仍待配置验收。', 'serve-mcp-delivery-20261116'),
}

def route_group(path, method):
    if path.startswith('/v1/chat/completions') and not (method == 'POST' and path == '/v1/chat/completions'):
        return 'stored'
    if path.startswith('/v1/conversations'): return 'conversations'
    if path.startswith('/v1/prompts'): return 'prompts'
    if path == '/v1/responses/compact': return 'compact'
    if path == '/v1/responses/input_tokens': return 'count'
    if path == '/v1/responses': return 'responses'
    if path.startswith('/v1/responses/'): return 'response_state'
    if path.startswith('/v1/files'): return 'files'
    if path.startswith('/v1/uploads'): return 'uploads'
    if path.startswith('/v1/batches'): return 'batches'
    if path == '/v1/moderations': return 'moderations'
    if path == '/v1/embeddings': return 'embeddings'
    if path.startswith('/v1/vector_stores'): return 'search' if path.endswith('/search') else 'vectors'
    if path.startswith('/v1/tunnels'): return 'tunnel'
    return None

for category in ('routes','native_routes'):
    for key in j[category]:
        method, path = key.split(' ',1)
        group = route_group(path, method)
        if method == 'OPTIONS':
            # A CORS preflight does not execute the corresponding application.
            update(category, key, '配置允许的实际路径/方法之原生CORS预检；Origin/method/header与Vary合同。',
                   '实际peer CIDR/Host仍检查；不执行模型、对象CRUD或分钟额度操作。后续请求仍需自己的Key/owner/当前准入绑定。',
                   (E+'serve-access-delivery-20261109/README.md',))
        elif group:
            semantics, boundary, evidence = groups[group]
            update(category,key,semantics,boundary,(E+evidence+'/README.md',))
        elif path.startswith('/v1/models'):
            if method == 'DELETE':
                semantics = '标准Models DELETE按真实资产权限处理：未知404，加载的只读基础模型403。'
                boundary = '没有伪造删除成功；用户自有训练模型管理归G00/A10未决/后续任务，模型目录只读。'
            else:
                semantics = '实际主模型/别名列表和详情，稳定创建时间；配置专用embedding时列出其独立标识。'
                boundary = '未知404、合法编码路径解码；别名不改变权重，当前模型目录不提供云训练资产管理。'
            update(category,key,semantics,boundary,(E+'serve-completions-20261108/README.md','src/server/chat_metrics.cpp'))
        elif path == '/v1/completions':
            update(category,key,'真实原始文本/token批量、echo/FIM、best_of、概率与JSON/SSE Completions。',
                   '已交付官方SDK/原生TLS/实际生成与取消后同PID恢复；输入/候选/usage按completion_fields完整合同。',
                   (E+'serve-completions-20261108/README.md',))
        elif category == 'routes' and path == '/healthz':
            update(category,key,boundary='已交付PLE永久故障传播；模型/PLE故障与真实设备故障分开，NVML/日志辅助失能不能伪装成GPU永久失效。')
        elif category == 'routes' and path == '/v1/capabilities':
            update(category,key,boundary='implementation_only，实际配置须结合/v1/config与发布身份。grammar可取消且有界清理，未回收占槽；局部资源限额不是整机RAM硬上限。')
        elif category == 'routes' and path == '/v1/chat/completions':
            update(category,key,'原生messages/raw prompt、真实采样/多choice/工具/schema、JSON/SSE，当前托管单槽媒体与文本MTP共存。',
                   '方法/参数/额度/内存准入及首帧后SSE错误按实际合同；动态压力下主轻恢复已验，剩余8个标准字段、stream选项及媒体缓存仍须实现。',
                   (E+'serve-media-managed-business-20261116/README.md', E+'serve-video-geometry-delivery-20261116/README.md'))

changed = {category:[key for key in j[category] if j[category][key] != before[category][key]]
           for category in ('fields','completion_fields','routes','native_routes')}
j['meaning'] = ('Current surface descriptions are manually reconciled against code and delivery; dated *_review/development sections retain historical evidence. Presence and source hashes do not certify compatibility. Current gaps remain implementation tasks in the master ledger.')
j['current_surface_review'] = dict(date='2026-11-16', baseline_commit='a362d68d',
    actual_default_revision=150, report=REPORT,
    rows_reviewed={category:len(j[category]) for category in changed},
    rows_changed={category:len(keys) for category,keys in changed.items()},
    runtime_source_changed=False, new_gpu_starts=0,
    global_semantics_certified=False, required_master_tasks_remain=True)
assert j['sources_sha256'] == before['sources_sha256']
assert j['whitelists'] == before['whitelists']
P.write_text(json.dumps(j,ensure_ascii=False,indent=2)+'\n')
(W/'changed-rows.json').write_text(json.dumps(changed,ensure_ascii=False,indent=2)+'\n')
(W/'original-inventory.sha256').write_text(hashlib.sha256(json.dumps(before,ensure_ascii=False,indent=2).encode()+b'\n').hexdigest()+'\n')
print(json.dumps(j['current_surface_review'],ensure_ascii=False))
