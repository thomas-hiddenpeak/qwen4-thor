# Agent接入：1长主agent＋3子代理、suffix与两级缓存

本例对应2026-10-29已合入main的交付程序6f3722b2。服务具备4个执行槽，
主agent可使用长槽，三个子代理通常使用短槽；主agent结束一次HTTP请求，
等待工具或子代理期间不占执行槽。服务进程保持运行，下一次请求携带完整
历史和原session_id恢复状态。槽位可以被别的请求使用，客户端不绑定槽号。

## 启动服务

在仓库根目录执行。两个缓存目录放在NVMe上的私有0700目录，不能放tmpfs。
目录需支持O_DIRECT/O_TMPFILE；缓存文件为匿名文件，不会显示为普通文件。

```bash
cd /home/rm01/models/dev/qwen4-thor
install -d -m 700 .q4t-work/agent-session-cache .q4t-work/agent-prefix-cache

./build/q4t serve \
  --model-dir /home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --host 127.0.0.1 --port 8000 \
  --max-seq 4 --max-prefill 4096 --max-len 208896 \
  --seq-capacities 208896,65536,65536,65536 \
  --mtp --mtp-k 3 --mtp-suffix \
  --session-cache-dir .q4t-work/agent-session-cache \
  --prefix-cache-dir .q4t-work/agent-prefix-cache \
  --prefix-cache-checkpoints 8192,32768,65536 \
  --request-deadline-ms 1200000
```

这条命令使用默认**完整head**。需要65536行BF16子集时，在同一条命令末尾加：

```text
--mtp-subset-head .q4t-work/prefix-seed-20261028/delivery-package/artifact
```

两种head使用相同客户端参数；切换head需要重启，进程内缓存随之失效。
服务就绪后可检查`curl --fail http://127.0.0.1:8000/healthz`，确认GPU健康、
4槽可用；模型名称可从`/v1/models`读取。不要为每个工具调用重启服务。

| 配置 | 含义 |
|---|---|
| 长槽208896 | **输入token＋请求max_tokens**不能超过208896 |
| 三个短槽65536 | 每路输入＋max_tokens不能超过65536；64K输入再要求输出会占长槽 |
| max-prefill4096 | 每块prefill暂存大小，不是输入上下文上限 |
| 4执行槽、32入口请求 | 更大突发由准入/排队承接，不是32路长请求同时生成或延迟保证 |
| 私有缓存 | 8项、24GiB SSD、32MiB RAM预算、30分钟TTL |
| 公共缓存 | 另8项、8GiB SSD、32MiB RAM预算、30分钟TTL |

共享内存预算不等于实际整机RAM增量，也不释放预分配的GPU槽内存。两个池
同进程有效，重启、过期、淘汰或不匹配时正常重新prefill。32个入口请求不
意味着32个会话快照都保持热缓存：每池最多8份，更多会话会触发淘汰。私有
请求租约另有32上限，同session并发也会409。客户端并发与排队策略应保持
有界。请求body上限16MiB、全局48MiB。

## 请求字段与正确顺序

`session_id`、`prefix_cache_scope`均由`secrets.token_hex(32)`生成，保存到
客户端会话状态中；不要每一轮重新生成。它们是能力凭据，不应跨不可信
用户共享。`request_id`和`cancel_token`则每次HTTP都重新生成。

| 字段 | 主agent首次请求 | 三个子代理首次请求 | 主agent恢复请求 |
|---|---|---|---|
| session_id | 主agent自己的ID | 每个子代理各自独立ID | **原主agent ID** |
| prefix_cache_scope | 同一可信共享组scope | 同一scope | 同一scope |
| prefix_cache_write_tokens | 例如8192，显式保存一个公共边界 | 0或省略，只读 | 0或省略，只读 |
| prompt | 公共前缀＋主私有历史＋主任务 | 公共前缀＋该子私有历史＋子任务 | 主原prompt＋完整输出＋新增工具/子结果 |
| temperature / top_p / n | 0 / 1 / 1 | 0 / 1 / 1 | 0 / 1 / 1 |
| stream / stream_options | true / include_usage=true | 同左 | 同左 |
| max_tokens | 本次输出预算，例如512 | 例如512，需计入短槽容量 | 例如512，需计入长槽容量 |

suffix由服务端`--mtp-suffix`启用，**没有客户端suffix开关**。它使用当前
请求的已提交历史生成候选，未命中时自动回到神经草稿；不会把另一个agent
的私有历史自动混入本请求。公共前缀必须实际出现在各自请求里。

第一次运行按以下顺序：

1. 主请求带正的`prefix_cache_write_tokens`，成功结束后发布公共种子并保存
   主私有会话。客户端消费完正常finish、usage、`[DONE]`和连接EOF。
2. 再并发发送三个子请求，各自独立session_id，同scope，公共写入设0。
   早于源请求成功结束的子请求可以正常生成，但可能miss，不能期待首轮共享。
3. 主agent在应用侧等待工具/子结果；不要挂住原生成连接来等待工具。
4. 主agent用原session_id提交**完整连续历史**及新增结果。私有会话优先，
   必要时再尝试公共前缀。子代理后续轮次同样复用各自ID和完整历史。

选边界P的规则：P必须在启动允许列表中，且P小于源请求实际输入token数；
所有希望命中的子请求，前P个**全局token ID**必须与源请求完全相同。
角色差异、动态时间、agent名称和私有历史放在公共内容之后。不能只数汉字、
字节或单独编码公共字符串：拼接处的分词也可能变化，应核对完整请求的
token最长公共前缀。已有较长私有会话跨过P时不会回算补种子；需要新种子时
可用新源会话或省略session_id，仍须正常成功完成这个源请求。

服务边界列表可配置1～4个严格递增整数，范围1024～131072，不锁死业务
输入长度。本例选择8K，亦可选32K；不能把65536种子用于总容量65536、还
需要输入尾部和输出的短槽。若公共内容不足8K，选择合适的小边界并同步
服务配置，或者只读miss；不要为了缓存给真实业务填充无用内容。收益验收
覆盖8K和32K，其他点只具备配置合同，不自动取得相同收益保证。
最终输入计数及实际命中仍以服务返回的usage为准。

## 可直接执行的完整Python示例

仓库提供[agent_cache_demo.py](../tools/examples/agent_cache_demo.py)。它使用
标准库HTTP客户端和本地tokenizer，完整发送**5条生成请求**：主保存1条、
三个子代理并发3条、主恢复1条。示例材料由CPU生成：主输入约96K，三个
子输入约33K/48K/60K，实际token数在准备时打印；公共token前缀也逐项核对。
这些重复档案是接线演示材料，不是新增性能验收，也不是业务必须采用的内容。

本机已配置的evalscope虚拟环境含transformers，可复用它只读加载本地
tokenizer；不会加载模型权重或访问网络下载模型。准备阶段不发HTTP：

```bash
# 目录必须是新目录，避免覆盖已有客户端会话、请求或响应。
tools/evalscope/.venv/bin/python -B tools/examples/agent_cache_demo.py prepare \
  --directory .q4t-work/agent-cache-example --boundary 8192

# 服务启动并就绪后再执行；此命令实际发送5条生成请求。
tools/evalscope/.venv/bin/python -B tools/examples/agent_cache_demo.py run \
  --directory .q4t-work/agent-cache-example --base-url http://127.0.0.1:8000
```

改成32K时，在新的目录`prepare --boundary 32768`；服务启动列表必须包含
32768。此小脚本只给8K/32K两组演示材料，服务本身的可配置范围如上。
默认材料路径和ID写入`state.json`；每条实际请求/响应分别保存，文件权限
由umask077保护。`run-started.json`阻止意外重复执行整批5请求；出错后检查
已完成的响应和服务状态，不要删除标记直接盲重放。应用应以每条成功响应
为事务边界保存历史，真实业务再按持久session_id逐轮推进。

例如`main-seed.request.json`中的真实请求体包含以下字段；示例代码自动填入
合法ID、完整长prompt和独立取消凭据，下面是字段结构而非可原样发送的ID：

```json
{
  "model": "qwen3.8-flash-next",
  "prompt": "完整预渲染prompt，不是只发新增内容",
  "max_tokens": 512,
  "temperature": 0,
  "top_p": 1,
  "n": 1,
  "stream": true,
  "stream_options": {"include_usage": true},
  "session_id": "主agent固定的64位小写hex",
  "prefix_cache_scope": "该共享组固定的64位小写hex",
  "prefix_cache_write_tokens": 8192,
  "request_id": "本条请求独有的字母数字短横线或下划线ID",
  "cancel_token": "本条请求新生成的64位小写hex",
  "request_timeout_ms": 1200000
}
```

脚本的`chat()`就是HTTP发送实现，`run()`展示会话生命周期：先等待主响应
完整结束，`ThreadPoolExecutor(max_workers=3)`调用三个子代理，最后拼接
原主prompt＋**未经strip或摘要的完整主输出**＋新增user轮次，恢复主会话。
工具执行和子代理调度由应用负责；示例把子报告作为协调器提供的文本结果，
没有自动执行模型生成的命令，也不包含工具调用解析器。主等待时不占槽，
是否有其他业务实际复用长槽由调度器决定，本例不额外制造干扰请求。

示例为了让演示输出有界，显式使用无thinking的assistant起始文本。真实应用
需要thinking时可以保留相应模板，但跨轮必须保存全部原始文本；不能只保存
最终答案再期待恢复完整已计算历史。`finish_reason=length`只是达到本轮输出
上限，不代表业务任务或工具调用已经完成，应由协调器决定继续生成或下一轮。

## 观察命中、错误及取消

看每条最终usage中的`prompt_tokens_details.cached_tokens`，示例会直接打印：

- 新scope的`main-seed`通常为0，表示源请求正常计算并在成功结束后发布。
- 三个新子会话在源种子存在、前缀相同且未过期时应各为8192（或32768）。
- `main-resumed`预期大于公共边界，恢复较长的主私有会话；最多少量旧尾部
  token会重新处理，不要求cached_tokens等于上一轮所有输入＋输出。

此usage字段是**实际恢复的目标前缀长度**，不分别返回私有/公共来源，也
不是suffix命中数。需要区分来源可看服务日志`[session-cache] event=restore`
的`source=session/shared`；公共写入见`[prefix-cache]`，suffix计数见
`[suffix]`请求摘要。0是合法miss，应用照常处理结果，不能因此盲目重试。
非GPU快照保存错误也可能只导致不发布缓存，生成仍成功；需看保存日志的error。
只读0只禁止公共写入；带session_id的子请求仍保存自己的私有快照。

流式HTTP 200不等于成功：要检查SSE中的error、正常finish、usage和`[DONE]`，
并读到EOF。示例`read_sse()`会拒绝错误、取消和不完整流；发生错误时不把
部分文本作为成功历史提交。服务的20分钟截止包含排队与生成，客户端socket
超时设为1260秒；这不是业务总耗时承诺，长任务按实际需求配置两端超时。

在另一个终端取消示例中的主请求（仅在它仍活动时有效）：

```bash
python3 - <<'PY'
import json
import urllib.request
from pathlib import Path

body = json.loads(Path('.q4t-work/agent-cache-example/main-seed.request.json').read_text())
cancel = {key: body[key] for key in ('request_id', 'cancel_token')}
request = urllib.request.Request(
    'http://127.0.0.1:8000/v1/requests/cancel',
    data=json.dumps(cancel).encode(), headers={'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=10) as response:
    print(response.status, response.read().decode())
PY
```

202仅表示取消已请求，不表示新快照已发布或槽立即释放；等待原请求退出。
已经结束或凭据不匹配会404。取消/失败不发布本轮新快照，不能靠取消一个
还在生成的请求来保存新的完整会话。主要接入错误如下：

| 现象 | 检查项 |
|---|---|
| 400 | 未开启相应缓存、ID格式不合法、P不在允许列表、P≥输入长度、输入＋输出预算超容量、非greedy参数或未知字段 |
| 409 | 同session并发/私有租约满，或request_id仍活动；同会话串行，确认原请求已结束再安排下一轮 |
| 503 | 入口容量满或GPU不健康；检查响应错误信息，按业务限流，设备错误按服务健康处理 |
| cached_tokens=0 | scope或原始token不同、没有成功种子、已过期/淘汰/重启；不是生成错误 |
| cached_tokens仅等于P | 可能只命中公共种子，检查主session_id及完整连续历史是否保留 |

## 已有messages客户端如何接入

上述三个缓存字段放在**HTTP请求JSON顶层**，不放进messages、工具定义或
chat_template_kwargs。`prompt`和`messages`二选一；raw prompt不能同时传
顶层tools或thinking模板选项。使用messages时，tools置于顶层；四路保持
相同公共system、工具定义及其顺序，把agent专属内容放在公共部分之后。

请求可以固定`chat_template_kwargs`中的`enable_thinking`、
`preserve_thinking`和`reasoning_effort`（low/medium/xhigh），但不能假设模板
重新渲染后仍命中：
当前模板会trim内容、处理thinking和工具序列化。当前响应把生成文本放在
content/delta.content，服务端没有自动拆出reasoning_content或tool_calls；
现有agent适配层需要自行解析，再按模板合同构建下一轮完整messages。
不要把包含`<think>`和工具文本的整段输出直接当最终content反复重包。
本例以raw prompt保持已验收的历史组织方式，不声称某个agent SDK即插即用。

已有客户端若过滤未知字段，需要确保它把session_id、prefix_cache_scope、
prefix_cache_write_tokens和取消/截止字段透传到服务；以最终发出的HTTP
JSON为准。服务不支持任意采样参数、结构化response_format或任意stop串，
不能直接照搬其他后端的所有参数。

业务迁移时替换演示材料与调度任务，保留这套ID生命周期、完整历史、精确
公共token匹配、成功发布和容量检查。原收益、负向观察、接受决定与回退
边界仍见[主线交付记录](SESSION_PREFIX_MAIN_MERGE_2026-10-29.md)。

## 本示例的检查范围

2026-10-29完成CPU准备检查：8K/32K两组实际输入均为
98304、33798、49161、60003 token，公共前缀及输入＋512输出容量检查通过。
5请求模拟检查覆盖源EOF后再启动三子、三子完成后恢复、ID分工和原文保留；
错误/取消/不完整SSE、HTTP409记录、length结束和防止意外整批重放也通过。
本次没有发送真实模型请求或新增GPU验证；已交付运行时及其验收证据复用。
见[CPU检查记录](evidence/agent-cache-integration-20261029/validation.json)。
