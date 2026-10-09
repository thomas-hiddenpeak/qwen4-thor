#!/usr/bin/env bash
# ============================================================================
# codex-local-model-setup.sh — 一键为 codex CLI 添加本地 vLLM 模型
#
# 方案 (openai/codex#46484 社区方案 + 本地适配):
#   codex 保持单一内置 openai provider, openai_base_url 指向本地 model router
#   (node 零依赖); router 按请求里的 model 名转发:
#     - MODEL_MATCH 前缀的请求 -> 本地 vLLM (免鉴权)
#     - 其余                   -> ChatGPT 后端 (透传登录态)
#   本地模型与 GPT 模型因此出现在同一个 TUI picker 里可自由切换。
#
# router 关键行为 (移植自本机实战, 全部有对应踩坑记录):
#   - 请求体智能解压: codex 客户端默认 zstd 压缩且不一定带 content-encoding 头,
#     依次探测 identity/zstd/brotli/gzip/deflate
#   - WS upgrade 一律回 426, 让 codex 回退 HTTPS
#   - 跨模型切换: 转发 ChatGPT 后端前剔除无 encrypted_content 的 reasoning 项
#     (本地 vLLM 产生的 reasoning 项会被后端 400/404 拒绝)
#   - V2 远程压缩: 本地路由收到 compaction_trigger 不转发, 直接合成 codex 期望的
#     compaction SSE 响应 (客户端本地重建历史, encrypted_content 是不透明 token);
#     后续请求带回的 compaction 项转发 vLLM 前剔除
#   - ChatGPT 路由缺 Authorization 时自动注入 ~/.codex/auth.json 的 access_token
#   - CODEX_ROUTER_LOG=1 落盘日志, CODEX_ROUTER_DUMP=<dir> 落盘完整请求/响应
#
# 脚本做的事 (幂等, 可重复执行):
#   1. 前置检查: node (>=23.8, 需 zstd) / codex / vLLM 可达 / ChatGPT 登录态
#   2. 安装 router 到 ~/.codex/model-router/codex-router.js
#   3. 安装 systemd user service codex-model-router (无 systemd 时回退 nohup)
#   4. 生成 ~/.codex/model-catalog.merged.json
#      (codex debug models 提取的内置 GPT 条目 + 本地模型条目)
#   5. 更新 ~/.codex/config.toml (先备份; 只动 4 个顶层 key + local_vlm 段)
#   6. 验证: router /health + 经 router 向本地模型发一次真实请求
#
# 用法:
#   ./codex-local-model-setup.sh                # 用默认参数
#   VLLM_URL=http://10.0.0.5:8000/v1 MODEL_NAME="MY-LLM" ./codex-local-model-setup.sh
#   ./codex-local-model-setup.sh --uninstall    # 停服务, 删 unit 与 router 目录
#
# 环境变量参数 (均可选):
#   VLLM_URL                  vLLM OpenAI 兼容端点   (默认 http://192.168.0.159:58000/v1)
#   MODEL_NAME                picker 里的模型名      (默认 "RM-01 VLM")
#   MODEL_MATCH               路由前缀, 默认 MODEL_NAME 首个空白分隔 token (默认 RM-01)
#   ROUTER_PORT               router 监听端口        (默认 4141)
#   CONTEXT_WINDOW            上下文窗口             (默认 262144)
#   AUTO_COMPACT_TOKEN_LIMIT  自动压缩阈值           (默认 212992)
#   CHATGPT_TARGET            兜底后端               (默认 https://chatgpt.com/backend-api/codex)
#
# 完成后: 杀掉旧 app-server 进程再开新 TUI 生效
#   (config 与 catalog 只在 app-server 进程启动时加载一次)。
# ============================================================================
set -euo pipefail

# ----------------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------------
VLLM_URL="${VLLM_URL:-http://192.168.0.159:58000/v1}"
MODEL_NAME="${MODEL_NAME:-RM-01 VLM}"
MODEL_MATCH="${MODEL_MATCH:-${MODEL_NAME%% *}}"
ROUTER_PORT="${ROUTER_PORT:-4141}"
CONTEXT_WINDOW="${CONTEXT_WINDOW:-262144}"
AUTO_COMPACT_TOKEN_LIMIT="${AUTO_COMPACT_TOKEN_LIMIT:-212992}"
CHATGPT_TARGET="${CHATGPT_TARGET:-https://chatgpt.com/backend-api/codex}"

CODEX_HOME_DIR="${HOME}/.codex"
ROUTER_DIR="${CODEX_HOME_DIR}/model-router"
ROUTER_JS="${ROUTER_DIR}/codex-router.js"
MERGED_CATALOG="${CODEX_HOME_DIR}/model-catalog.merged.json"
CONFIG_TOML="${CODEX_HOME_DIR}/config.toml"
UNIT_NAME="codex-model-router.service"
UNIT_PATH="${HOME}/.config/systemd/user/${UNIT_NAME}"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
die()  { printf '\033[1;31m错误: %s\033[0m\n' "$*" >&2; exit 1; }
warn() { printf '\033[1;33m警告: %s\033[0m\n' "$*" >&2; }

# ----------------------------------------------------------------------------
# --help / --uninstall
# ----------------------------------------------------------------------------
case "${1:-}" in
  -h|--help)
    sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  --uninstall)
    step "卸载 model router"
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now "${UNIT_NAME}" 2>/dev/null || true
      rm -f "${UNIT_PATH}"
      systemctl --user daemon-reload 2>/dev/null || true
      echo "已停止并移除 systemd user service: ${UNIT_NAME}"
    else
      pkill -f "node .*codex-router\.js" 2>/dev/null && echo "已杀掉 nohup 拉起的 router 进程" || true
    fi
    rm -rf "${ROUTER_DIR}"
    echo "已删除 ${ROUTER_DIR}"
    cat <<EOF

config.toml 需手动移除以下条目 (脚本不自动改, 有备份可对照):
  model = "${MODEL_NAME}"
  model_provider = "openai"
  openai_base_url = "http://127.0.0.1:${ROUTER_PORT}/v1"
  model_catalog_json = "~/.codex/model-catalog.merged.json"
  [model_providers.local_vlm] 整段

${MERGED_CATALOG} 可保留也可删除 (不影响其他模型)。
EOF
    exit 0
    ;;
esac

# ----------------------------------------------------------------------------
# 1. 前置检查
# ----------------------------------------------------------------------------
step "前置检查"

NODE_BIN="$(command -v node || true)"
[ -n "${NODE_BIN}" ] || die "未找到 node, 请先安装 (nvm install 24 推荐)"
"${NODE_BIN}" -e 'const z=require("zlib");z.zstdDecompressSync(z.zstdCompressSync(Buffer.from("{}")))' \
  || die "node 缺少 zstd 支持, 需要 Node >= 23.8 (推荐 24+): 当前 $(${NODE_BIN} --version)"
echo "node: ${NODE_BIN} ($(${NODE_BIN} --version))"

command -v codex >/dev/null 2>&1 || die "未找到 codex CLI, 请先安装 (npm i -g @openai/codex)"
echo "codex: $(codex --version 2>/dev/null || echo 未知版本)"

# vLLM 可达性
VLLM_BASE="${VLLM_URL%/}"
"${NODE_BIN}" -e '
const http=require("http");
const u=new URL(process.argv[1]+"/models");
const r=http.request(u,{method:"GET"},res=>{
  let d="";res.on("data",c=>d+=c);res.on("end",()=>{
    if(res.statusCode!==200){console.error("  vLLM /models 返回",res.statusCode,d.slice(0,120));process.exit(1)}
    try{const j=JSON.parse(d);console.log("vLLM 可达, 模型:",j.data.map(m=>m.id).join(", "))}
    catch(e){console.log("vLLM 可达 (响应非 JSON)")}
    process.exit(0);
  });
});
r.on("error",e=>{console.error("  vLLM 不可达:",e.message);process.exit(1)});
r.setTimeout(5000,()=>{console.error("  vLLM 连接超时");process.exit(1)});
r.end();
' "${VLLM_BASE}" || die "vLLM 端点不可用: ${VLLM_URL}"

# ChatGPT 登录态 (兜底路由需要; 缺失只警告不阻断)
if [ -f "${CODEX_HOME_DIR}/auth.json" ] && grep -q '"access_token"' "${CODEX_HOME_DIR}/auth.json" 2>/dev/null; then
  echo "ChatGPT 登录态: 已找到 (~/.codex/auth.json)"
else
  warn "未找到 ChatGPT 登录态 (~/.codex/auth.json), 兜底路由到 GPT 模型会失败; 先运行 codex login"
fi

# 端口占用检查 (非本 router 占用则报错)
if "${NODE_BIN}" -e '
const net=require("net");
const s=net.createServer();
s.once("error",()=>process.exit(1));
s.listen('"${ROUTER_PORT}"',"127.0.0.1",()=>{s.close(()=>process.exit(0))});
' 2>/dev/null; then
  echo "端口 ${ROUTER_PORT}: 空闲"
else
  curl -fsS "http://127.0.0.1:${ROUTER_PORT}/health" >/dev/null 2>&1 \
    && echo "端口 ${ROUTER_PORT}: 已被本 router 占用 (将重启)" \
    || die "端口 ${ROUTER_PORT} 被其他进程占用"
fi

# ----------------------------------------------------------------------------
# 2. 安装 router
# ----------------------------------------------------------------------------
step "安装 router -> ${ROUTER_JS}"
mkdir -p "${ROUTER_DIR}/dump"

cat > "${ROUTER_JS}.tmp" <<'ROUTER_JS_EOF'
#!/usr/bin/env node
/**
 * codex-model-router (基于 openai/codex#46484 社区方案)
 *
 * 作用: 让 codex 保持单一 provider (内置 openai), 把 openai_base_url 指向本 router;
 *       每个请求都带 model name, router 按名字转发到对应后端, 从而让
 *       本地 vLLM 模型与 OpenAI/ChatGPT 模型出现在同一个 picker 里可切换。
 *
 * 路由规则 (routes 数组, 首个匹配 model 前缀者胜, "" 为兜底):
 *   - 本地前缀 -> 本地 vLLM (免鉴权)
 *   - ""       -> OpenAI/ChatGPT 后端 (透传 codex 发送的 Authorization)
 *
 * 关键行为:
 *   - WebSocket upgrade 一律回 426, 让 codex 回退到 HTTPS (上游均不走 ws)
 *   - 请求体先缓冲以读取 model 字段, 响应流式透传
 *
 * 调试: 设 CODEX_ROUTER_LOG=1 打印每行请求日志
 */
'use strict';

const http = require('http');
const https = require('https');
const zlib = require('zlib');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const PORT = parseInt(process.env.CODEX_ROUTER_PORT || '4141', 10);
const HOST = '127.0.0.1';
const LOG = process.env.CODEX_ROUTER_LOG === '1';
// 设 CODEX_ROUTER_DUMP=<dir> 后, 每个请求的完整 body 与上游响应落盘到该目录
const DUMP_DIR = process.env.CODEX_ROUTER_DUMP || '';

const routes = [
  {
    match: '__MODEL_MATCH__',
    target: '__VLLM_URL__',
    passthroughAuth: false, // 本地 vLLM 免鉴权, 去掉 Authorization
  },
  {
    match: '',
    // codex 请求 /v1/responses -> 去掉 /v1 -> /responses
    // 目标 = CHATGPT_TARGET (后端真实路径不带 /v1)
    target: '__CHATGPT_TARGET__',
    passthroughAuth: true, // 透传 codex 的登录态
  },
];

function pickRoute(model) {
  for (const r of routes) {
    if (model && model.startsWith(r.match)) return r;
  }
  return routes[routes.length - 1];
}

function logLine(msg) {
  if (LOG) console.log(new Date().toISOString() + ' ' + msg);
}

// 读取 codex ChatGPT 登录态 access_token (带简单缓存, 文件变更时重读)
let _tokCache = { mtime: 0, token: '' };
function chatgptAccessToken() {
  try {
    const p = require('os').homedir() + '/.codex/auth.json';
    const st = fs.statSync(p);
    if (_tokCache.mtime !== st.mtimeMs) {
      const a = JSON.parse(fs.readFileSync(p, 'utf8'));
      _tokCache = { mtime: st.mtimeMs, token: (a.tokens && a.tokens.access_token) || '' };
    }
    return _tokCache.token;
  } catch (e) {
    return '';
  }
}

// 智能解压请求体: codex 客户端可能压缩 body 且不一定带 content-encoding 头。
// 依次尝试 identity / zstd / brotli / gzip / deflate, 返回第一个能解析为 JSON 的结果。
function decodeBody(raw) {
  if (!raw.length) return { body: raw, method: 'empty' };
  const candidates = [
    ['identity', (b) => b],
    ['zstd', (b) => zlib.zstdDecompressSync(b)], // codex 客户端默认用 zstd
    ['brotli', (b) => zlib.brotliDecompressSync(b)],
    ['gzip', (b) => zlib.gunzipSync(b)],
    ['deflate', (b) => zlib.inflateSync(b)],
    ['deflateRaw', (b) => zlib.inflateRawSync(b)],
  ];
  for (const [name, fn] of candidates) {
    try {
      const out = fn(raw);
      JSON.parse(out.toString('utf8')); // 验证是有效 JSON
      return { body: out, method: name };
    } catch (e) {
      /* 该算法失败, 继续尝试 */
    }
  }
  return { body: raw, method: 'unrecognized' };
}

// 路由到 ChatGPT 后端前, 剔除 input 历史中的"外来 reasoning 项"。
// ChatGPT 后端只认自己持久化过的 reasoning 项 (带 encrypted_content);
// 本地 vLLM 产生的 reasoning 项 (无 encrypted_content) 会被拒绝 (400 unknown_parameter
// 或 404 item not found)。message 项不引用 reasoning id, 删除是安全的。
// 仅对透传鉴权的 ChatGPT 后端生效, 本地 vLLM 保留自己的 reasoning 项。
function stripForeignReasoning(body) {
  let j;
  try {
    j = JSON.parse(body.toString('utf8'));
  } catch (e) {
    return body; // 非 JSON, 原样
  }
  if (!Array.isArray(j.input)) return body;
  const before = j.input.length;
  j.input = j.input.filter(
    (it) => !(it && it.type === 'reasoning' && !it.encrypted_content)
  );
  const removed = before - j.input.length;
  if (removed > 0) {
    logLine(`stripped ${removed} foreign reasoning item(s) for chatgpt backend`);
    return Buffer.from(JSON.stringify(j), 'utf8');
  }
  return body;
}

// codex 对 openai provider 判定支持 V2 远程压缩 (源码 provider.rs: is_openai() → V2),
// 上下文快满时在 input 末尾追加 {"type":"compaction_trigger"} 让后端执行压缩。
// V2 协议要求后端返回 SSE 流, 其中恰好一个 {"type":"compaction","encrypted_content":...}
// 输出项 (collect_compaction_output 校验 compaction_count==1, 否则 fatal)。
// 本地 vLLM 不认 compaction_trigger 会 400, 也给不出 compaction 项。
// 关键: 客户端 build_v2_compacted_history 在本地做历史压缩 (按保留规则过滤 input +
// 截断到 token 预算 + 追加 compaction 项), 后端的 encrypted_content 对客户端是不透明
// token (只存回、下次请求带回)。所以 router 直接合成 compaction 响应即可, 无需 vLLM。
function hasCompactionTrigger(body) {
  try {
    const j = JSON.parse(body.toString('utf8'));
    return Array.isArray(j.input) && j.input.some((it) => it && it.type === 'compaction_trigger');
  } catch (e) {
    return false;
  }
}

// 合成 codex 期望的 V2 compaction SSE 响应 (不转发给 vLLM)
function respondSyntheticCompaction(req, res, model) {
  const respId = 'resp_router_compact_' + Date.now().toString(36);
  const sse =
    'event: response.output_item.done\n' +
    'data: ' +
    JSON.stringify({
      type: 'response.output_item.done',
      item: { type: 'compaction', encrypted_content: 'router_local_compaction_v1' },
    }) +
    '\n\n' +
    'event: response.completed\n' +
    'data: ' +
    JSON.stringify({
      type: 'response.completed',
      response: {
        id: respId,
        object: 'response',
        model: model || '',
        status: 'completed',
        output: [{ type: 'compaction', encrypted_content: 'router_local_compaction_v1' }],
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          total_tokens: 0,
          cached_input_tokens: 0,
        },
      },
    }) +
    '\n\n';
  res.writeHead(200, {
    'content-type': 'text/event-stream',
    'cache-control': 'no-cache',
    connection: 'keep-alive',
  });
  res.end(sse);
  logLine(`SYNTHETIC compaction response for local vllm model=${model || '-'} (${req.method} ${req.url})`);
}

// 后续请求会把 compaction 项 (含 encrypted_content) 带回 input; 本地 vLLM 不认会 400,
// 转发前剔除。客户端本地已用该项重建历史, 剔除不影响语义。
function stripCompactionItem(body) {
  let j;
  try {
    j = JSON.parse(body.toString('utf8'));
  } catch (e) {
    return body;
  }
  if (!Array.isArray(j.input)) return body;
  const before = j.input.length;
  j.input = j.input.filter((it) => !(it && it.type === 'compaction'));
  const removed = before - j.input.length;
  if (removed > 0) {
    logLine(`stripped ${removed} compaction item(s) for local vllm`);
    return Buffer.from(JSON.stringify(j), 'utf8');
  }
  return body;
}

function forward(req, res, body, model, dumpBase) {
  const route = pickRoute(model);
  const fwdPath = req.url.replace(/^\/v1/, '') || '/';
  let targetUrl;
  try {
    targetUrl = new URL(route.target + fwdPath);
  } catch (e) {
    res.writeHead(500, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'bad route target: ' + e.message } }));
    return;
  }

  // ChatGPT 后端不认外来 reasoning 项, 转发前剔除
  if (route.passthroughAuth) {
    body = stripForeignReasoning(body);
  } else {
    // 本地 vLLM 不支持 V2 远程压缩:
    // 1) 压缩请求 (带 compaction_trigger) 不转发, 直接合成 codex 期望的 compaction 响应
    // 2) 后续请求带回的 compaction 项, 转发前剔除
    if (hasCompactionTrigger(body)) {
      respondSyntheticCompaction(req, res, model);
      return;
    }
    body = stripCompactionItem(body);
  }

  const headers = {};
  for (const [k, v] of Object.entries(req.headers)) {
    // 请求体已解压为明文, 去掉 content-encoding 避免上游误解码
    if (k === 'host' || k === 'content-length' || k === 'connection' || k === 'content-encoding') continue;
    headers[k] = v;
  }
  if (!route.passthroughAuth) delete headers.authorization;
  // ChatGPT 后端必须有登录态: 若客户端没带 Authorization (如 local_vlm 免鉴权
  // provider 发出的请求), 自动补 auth.json 里的 access_token
  if (route.passthroughAuth && !headers.authorization) {
    const tok = chatgptAccessToken();
    if (tok) {
      headers.authorization = 'Bearer ' + tok;
      logLine(`injected access_token for chatgpt backend (request had no Authorization)`);
    }
  }
  if (LOG) {
    logLine(
      `auth=${headers.authorization ? 'present(' + String(headers.authorization).slice(0, 15) + '...)' : 'MISSING'}`
    );
  }
  headers['content-length'] = String(body.length);

  const lib = targetUrl.protocol === 'https:' ? https : http;
  const t0 = Date.now();
  const fwd = lib.request(targetUrl, { method: req.method, headers }, (upstream) => {
    res.writeHead(upstream.statusCode, upstream.headers);
    if (DUMP_DIR && dumpBase) {
      const respChunks = [];
      upstream.on('data', (c) => respChunks.push(c));
      upstream.on('end', () => {
        try {
          fs.writeFileSync(
            path.join(DUMP_DIR, `${dumpBase}.resp.${upstream.statusCode}`),
            Buffer.concat(respChunks)
          );
        } catch (e) {
          /* 落盘失败不影响转发 */
        }
      });
    }
    upstream.pipe(res);
    logLine(
      `${req.method} ${req.url} model=${model || '-'} -> ${targetUrl.host} ` +
        `${upstream.statusCode} ${Date.now() - t0}ms`
    );
  });
  fwd.on('error', (e) => {
    logLine(`${req.method} ${req.url} model=${model || '-'} -> ${targetUrl.host} ERROR ${e.message}`);
    if (!res.headersSent) {
      res.writeHead(502, { 'content-type': 'application/json' });
    }
    res.end(JSON.stringify({ error: { message: 'upstream error: ' + e.message } }));
  });
  fwd.write(body);
  fwd.end();
}

const server = http.createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ ok: true, routes: routes.map((r) => r.target) }));
    return;
  }

  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const raw = Buffer.concat(chunks);
    const { body, method } = decodeBody(raw);
    let model = '';
    if (body.length) {
      try {
        model = JSON.parse(body.toString('utf8')).model || '';
      } catch (e) {
        /* 非 JSON 或无 model 字段, 走兜底路由 */
      }
    }
    if (method !== 'identity' && method !== 'empty') {
      logLine(`decoded body ${raw.length} -> ${body.length} bytes (method=${method}, header-encoding=${req.headers['content-encoding'] || 'none'})`);
    }
    if (LOG && body.length) {
      const preview = body.toString('utf8', 0, Math.min(body.length, 200));
      logLine(`body(${body.length}B) model="${model}" preview=${preview}`);
    }
    let dumpBase = '';
    if (DUMP_DIR && body.length) {
      dumpBase = `${Date.now()}_${(model || 'nomodel').replace(/[^A-Za-z0-9._-]/g, '_')}`;
      try {
        fs.writeFileSync(path.join(DUMP_DIR, `${dumpBase}.req.json`), body);
        logLine(`dumped request -> ${dumpBase}.req.json`);
      } catch (e) {
        logLine(`dump error: ${e.message}`);
        dumpBase = '';
      }
    }
    forward(req, res, body, model, dumpBase);
  });
  req.on('error', (e) => {
    logLine(`request error: ${e.message}`);
    if (!res.headersSent) res.writeHead(400);
    res.end();
  });
});

// WebSocket upgrade -> 426, 促使 codex 回退 HTTPS
server.on('upgrade', (req, socket) => {
  logLine(`WS upgrade ${req.url} -> 426`);
  socket.write('HTTP/1.1 426 Upgrade Required\r\nConnection: close\r\n\r\n');
  socket.destroy();
});

server.listen(PORT, HOST, () => {
  console.log(`codex-model-router listening on http://${HOST}:${PORT}`);
});
ROUTER_JS_EOF

# 填充路由参数
sed -e "s|__VLLM_URL__|${VLLM_URL}|g" \
    -e "s|__MODEL_MATCH__|${MODEL_MATCH}|g" \
    -e "s|__CHATGPT_TARGET__|${CHATGPT_TARGET}|g" \
    "${ROUTER_JS}.tmp" > "${ROUTER_JS}"
rm -f "${ROUTER_JS}.tmp"
"${NODE_BIN}" --check "${ROUTER_JS}" || die "router JS 语法检查失败"
echo "router 已安装 (model '${MODEL_NAME}' 前缀 '${MODEL_MATCH}' -> ${VLLM_URL})"

# ----------------------------------------------------------------------------
# 3. 安装并启动服务
# ----------------------------------------------------------------------------
step "安装服务"
USE_SYSTEMD=false
# 判断 systemd user manager 是否可用: 看 user manager 的私有 socket 是否存在。
# 注意不能用 is-system-running (degraded/starting 等瞬态也返回非零, 会误判)。
if command -v systemctl >/dev/null 2>&1 \
  && [ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/private" ]; then
  USE_SYSTEMD=true
  mkdir -p "$(dirname "${UNIT_PATH}")"
  cat > "${UNIT_PATH}" <<UNIT_EOF
[Unit]
Description=Codex model router (local vLLM + OpenAI/ChatGPT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${NODE_BIN} ${ROUTER_JS}
Environment=CODEX_ROUTER_PORT=${ROUTER_PORT}
Environment=CODEX_ROUTER_LOG=1
Environment=CODEX_ROUTER_DUMP=${ROUTER_DIR}/dump
StandardOutput=append:${ROUTER_DIR}/router.log
StandardError=append:${ROUTER_DIR}/router.log
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT_EOF
  systemctl --user daemon-reload
  systemctl --user stop "${UNIT_NAME}" 2>/dev/null || true
  pkill -f "node .*codex-router\.js" 2>/dev/null || true   # 清理 nohup 回退留下的游离进程
  sleep 1
  systemctl --user enable "${UNIT_NAME}"
  systemctl --user start "${UNIT_NAME}"
  echo "systemd user service 已启用并启动: ${UNIT_NAME}"
  echo "日志: ${ROUTER_DIR}/router.log"
  # 提示 linger (登出后服务保持运行)
  if command -v loginctl >/dev/null 2>&1 && ! loginctl show-user "${USER}" 2>/dev/null | grep -q 'Linger=yes'; then
    warn "未开启 linger, 登出后服务会停止; 可运行: sudo loginctl enable-linger ${USER}"
  fi
else
  warn "systemd user 不可用, 回退 nohup (重启/登出后需手动拉起)"
  pkill -f "node .*codex-router\.js" 2>/dev/null || true
  sleep 1
  nohup "${NODE_BIN}" "${ROUTER_JS}" >> "${ROUTER_DIR}/router.log" 2>&1 &
  echo "router 已用 nohup 拉起 (pid $!)"
fi
sleep 1

# ----------------------------------------------------------------------------
# 4. 生成合并 catalog
# ----------------------------------------------------------------------------
step "生成合并 catalog -> ${MERGED_CATALOG}"
# 用空 CODEX_HOME 提取纯内置 catalog (避免读到已合并的旧版), 随 codex 版本自动更新
BUILTIN_TMP="$(mktemp -d "${HOME}/.codex/.builtin-catalog.XXXXXX")"
trap 'rm -rf "${BUILTIN_TMP}"' EXIT
CODEX_HOME="${BUILTIN_TMP}" codex debug models 2>/dev/null > "${BUILTIN_TMP}/builtin.json" \
  || die "codex debug models 提取内置 catalog 失败 (需要较新版本的 codex CLI)"

"${NODE_BIN}" - "${BUILTIN_TMP}/builtin.json" "${MERGED_CATALOG}" \
  "${MODEL_NAME}" "${CONTEXT_WINDOW}" "${AUTO_COMPACT_TOKEN_LIMIT}" <<'NODE_MERGE_EOF'
const fs = require('fs');
const [src, out, slug, ctx, compact] = process.argv.slice(2);
const j = JSON.parse(fs.readFileSync(src, 'utf8'));
const models = (j.models || []).filter((m) => m.slug !== slug);
models.push({
  slug: slug,
  display_name: slug,
  description: 'Local vLLM deployment routed through codex-model-router.',
  default_reasoning_level: 'medium',
  supported_reasoning_levels: [
    { effort: 'none', description: '关闭 thinking' },
    { effort: 'low', description: '简短、聚焦的思考' },
    { effort: 'medium', description: '正常思考; 日常编程默认档位' },
    { effort: 'xhigh', description: '更深入地验证假设与比较方案' },
  ],
  context_window: Number(ctx),
  max_context_window: Number(ctx),
  effective_context_window_percent: 95,
  auto_compact_token_limit: Number(compact),
  input_modalities: ['text', 'image'],
  supports_image_detail_original: false,
  shell_type: 'unified_exec',
  tool_mode: 'direct',
  visibility: 'list',
  supported_in_api: true,
  priority: 1,
  support_verbosity: false,
  supports_reasoning_summary_parameter: false,
  default_reasoning_summary: 'none',
  supports_reasoning_effort_updates: false,
  supports_experimental_context: false,
  use_responses_lite: false,
  include_skills_usage_instructions: true,
  include_plugin_usage_instructions: true,
  experimental_supported_tools: [],
  truncation_policy: { mode: 'tokens', limit: 10000 },
  model_messages: {
    instructions_template:
      'You are a coding agent running in Codex CLI. Follow the user\'s task and applicable repository instructions. Inspect relevant files before changing them. Use only the provided tools and their declared schemas. Make focused changes, preserve unrelated user work, and respect sandbox and approval requirements. Use targeted checks to diagnose failures and validate changes. Do not claim commands ran or tests passed without tool evidence. Continue through implementation and verification when requested, then report the outcome and any limitations in the user\'s language.',
  },
});
j.models = models;
fs.writeFileSync(out, JSON.stringify(j, null, 2) + '\n');
console.log('catalog 已生成: ' + models.length + ' 个模型 (' + models.map((m) => m.slug).join(', ') + ')');
NODE_MERGE_EOF

# ----------------------------------------------------------------------------
# 5. 更新 config.toml
# ----------------------------------------------------------------------------
step "更新 ${CONFIG_TOML}"
[ -f "${CONFIG_TOML}" ] || die "未找到 ${CONFIG_TOML}, 请先运行一次 codex 生成配置"
BACKUP="${CONFIG_TOML}.bak-$(date +%Y%m%d-%H%M%S)"
cp "${CONFIG_TOML}" "${BACKUP}"
echo "已备份 -> ${BACKUP}"

"${NODE_BIN}" - "${CONFIG_TOML}" "${ROUTER_PORT}" "${MODEL_NAME}" "${MERGED_CATALOG}" <<'NODE_TOML_EOF'
// 行级 TOML 合并: 设置 4 个顶层 key + 替换/追加 [model_providers.local_vlm] 段
const fs = require('fs');
const [file, port, model, catalog] = process.argv.slice(2);
const keys = {
  model: model,
  model_provider: 'openai',
  openai_base_url: `http://127.0.0.1:${port}/v1`,
  model_catalog_json: '~/.codex/' + catalog.split('/').pop(),
};
const sectionName = 'model_providers.local_vlm';
const sectionLines = [
  `[${sectionName}]`,
  'name = "local_vlm"',
  `base_url = "http://127.0.0.1:${port}/v1"`,
  'wire_api = "responses"',
];
let lines = fs.readFileSync(file, 'utf8').split('\n');

// 顶层 key: 只处理第一个 [section] 之前的行
for (const [k, v] of Object.entries(keys)) {
  const firstSection = lines.findIndex((l) => /^\s*\[/.test(l));
  const top = firstSection === -1 ? lines.length : firstSection;
  const re = new RegExp('^' + k + '\\s*=');
  const idx = lines.slice(0, top).findIndex((l) => re.test(l));
  const line = k + ' = ' + JSON.stringify(v);
  if (idx !== -1) lines[idx] = line;
  else lines.splice(top, 0, line);
}

// 段: 整段替换或追加到文件末尾
const secRe = new RegExp('^\\[' + sectionName.replace(/\./g, '\\.') + '\\]\\s*$');
const sIdx = lines.findIndex((l) => secRe.test(l));
if (sIdx !== -1) {
  let eIdx = lines.findIndex((l, i) => i > sIdx && /^\s*\[/.test(l));
  if (eIdx === -1) eIdx = lines.length;
  lines.splice(sIdx, eIdx - sIdx, ...sectionLines);
} else {
  if (lines.length && lines[lines.length - 1] !== '') lines.push('');
  lines.push(...sectionLines);
}
fs.writeFileSync(file, lines.join('\n'));
console.log('config.toml 已更新: ' + Object.keys(keys).join(', ') + ' + [' + sectionName + ']');
NODE_TOML_EOF

# ----------------------------------------------------------------------------
# 6. 验证
# ----------------------------------------------------------------------------
step "验证"
HEALTH="$(curl -fsS "http://127.0.0.1:${ROUTER_PORT}/health" 2>/dev/null)" \
  || die "router /health 不通, 查看 ${ROUTER_DIR}/router.log"
echo "health: ${HEALTH}"

echo "经 router 向本地模型发测试请求..."
"${NODE_BIN}" - "${MODEL_NAME}" "${ROUTER_PORT}" <<'NODE_TEST_EOF'
const http = require('http');
const [model, port] = process.argv.slice(2);
const body = JSON.stringify({
  model: model,
  input: [{ type: 'message', role: 'user', content: [{ type: 'input_text', text: 'ping' }] }],
  max_output_tokens: 16,
});
const r = http.request(
  { host: '127.0.0.1', port: Number(port), path: '/v1/responses', method: 'POST',
    headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) } },
  (res) => {
    let d = '';
    res.on('data', (c) => (d += c));
    res.on('end', () => {
      console.log('  状态: ' + res.statusCode + '  响应: ' + d.slice(0, 120).replace(/\n/g, ' '));
      process.exit(res.statusCode === 200 ? 0 : 1);
    });
  }
);
r.on('error', (e) => { console.error('  请求失败: ' + e.message); process.exit(1); });
r.setTimeout(60000, () => { console.error('  超时'); process.exit(1); });
r.end(body);
NODE_TEST_EOF
echo "本地模型链路 OK"

# ----------------------------------------------------------------------------
# 完成
# ----------------------------------------------------------------------------
step "完成"
cat <<EOF
配置摘要:
  本地模型   : ${MODEL_NAME} (前缀 ${MODEL_MATCH}) -> ${VLLM_URL}
  兜底后端   : ${CHATGPT_TARGET}
  router     : http://127.0.0.1:${ROUTER_PORT}/v1
  catalog    : ${MERGED_CATALOG}
  config 备份: ${BACKUP}

下一步 (让新配置生效):
  1. 退出当前 TUI
  2. 杀掉旧 app-server 进程 (config/catalog 只在进程启动时加载):
       pkill -f 'codex.*app-server' ; pkill -f code-mode-host
  3. 重新启动: codex (或你的启动方式, 如 codex --yolo)

常用排查:
  服务状态 : systemctl --user status codex-model-router
  日志     : tail -f ${ROUTER_DIR}/router.log
  请求落盘 : ${ROUTER_DIR}/dump/
  卸载     : $0 --uninstall
EOF
