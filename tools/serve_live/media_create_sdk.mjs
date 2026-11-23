// One local TLS request through the pinned official SDK. No retries.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { check } from '../serve_realtime/conversation_checks.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  check(Buffer.byteLength(input) <= 131072, 'bounded Live startup');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR, 'node_modules/openai');
check(JSON.parse(fs.readFileSync(path.join(sdk, 'package.json'))).version === '7.47.0', 'SDK identity');
const endpoint = new URL(args.base_url);
check(endpoint.protocol === 'https:' && endpoint.hostname === '127.0.0.1' &&
      endpoint.pathname === '/v1' && !endpoint.username && !endpoint.password &&
      !endpoint.search && !endpoint.hash, 'local test endpoint');
const { default: OpenAI } = await import(pathToFileURL(path.join(sdk, 'index.mjs')));
const client = new OpenAI({ baseURL: endpoint.toString(), apiKey: args.key, maxRetries: 0, timeout: 20000 });
try {
  if (args.action === 'create' || args.action === 'credential-call') {
    let account = client;
    if (args.action === 'credential-call') {
      const grant = await client.realtime.clientSecrets.create({session:args.credential});
      check(grant.session.type === args.credential.type, 'credential resource type');
      account = new OpenAI({baseURL:endpoint.toString(), apiKey:grant.value, maxRetries:0, timeout:20000});
    }
    const response = await account.realtime.calls.create(args.request);
    console.log(JSON.stringify({status:response.status, sdp:await response.text(),
      mime:response.headers.get('content-type'), location:response.headers.get('location')}));
  } else if (args.action === 'hangup') {
    const response = await client.realtime.calls.hangup(args.id).asResponse();
    console.log(JSON.stringify({status:response.status, body:await response.text()}));
  } else if (args.action === 'attach' || args.action === 'credential-session') {
    const {OpenAIRealtimeWS} = await import(pathToFileURL(path.join(sdk,'realtime/ws.mjs')));
    const target = args.action === 'attach' ? {callID:args.id} : {model:'qwen3.8-flash-next'};
    const observer = new OpenAIRealtimeWS({...target, options:{handshakeTimeout:5000,
      perMessageDeflate:false, followRedirects:false, maxPayload:8<<20}},client);
    try {
      const result = await new Promise((resolve,reject)=>{
        const timer = setTimeout(()=>reject(new Error('sideband snapshot deadline')),8000);
        function finish(value) { clearTimeout(timer); resolve(value); }
        observer.socket.on('unexpected-response',(_,res)=>{
          res.resume();
          finish({status:res.statusCode});
        });
        observer.on('event', event=>{
          if (event.type === 'session.created')
            finish({status:101, session:event.session.id, instructions:event.session.instructions});
        });
        observer.on('error',error=>{clearTimeout(timer); reject(error);});
      });
      console.log(JSON.stringify(result));
    } finally {
      if (observer.socket.readyState === 1) observer.close();
      else observer.socket.terminate();
    }
  } else {
    const pending = args.source ? client.live.sessions.fork(args.source, args.request) : client.live.create(args.request);
    const { response, data } = await pending.withResponse();
    console.log(JSON.stringify({status: response.status, data}));
  }
} catch (error) {
  if (!Number.isInteger(error.status)) throw error;
  console.log(JSON.stringify({status: error.status, code: error.code}));
}
