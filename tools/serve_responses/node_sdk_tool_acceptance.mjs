import assert from 'node:assert/strict';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const cfg = JSON.parse(readFileSync(0, 'utf8'));
const oldSdk = resolve(cfg.legacy);
const newSdk = resolve(cfg.modern);
const version = dir => JSON.parse(readFileSync(resolve(dir,'package.json'))).version;
assert.equal(version(oldSdk),'6.34.0');
assert.equal(version(newSdk),'7.47.0');
const captures = new Map();
const limit = 4 * 1024 * 1024;
async function captureResponse(response, request) {
  const body = response.clone().body.getReader();
  const chunks = [];
  let bytes = 0;
  try {
    for (;;) {
      const {value, done} = await body.read();
      if (done) break;
      bytes += value.byteLength;
      assert.ok(bytes <= limit, 'wire capture exceeds limit');
      chunks.push(Buffer.from(value));
    }
  } finally {
    body.releaseLock();
  }
  return {...request, status: response.status, type: response.headers.get('content-type'),
          body: new TextDecoder('utf-8', {fatal: true}).decode(Buffer.concat(chunks))};
}

async function client(dir, key, reader) {
  const {default: OpenAI} = await import(pathToFileURL(resolve(dir, 'index.mjs')));
  return new OpenAI({
    apiKey: key, baseURL: cfg.base, timeout: 70000, maxRetries: 0,
    fetch: async (url, init) => {
      const response = await fetch(url, init);
      const uri = new URL(url);
      const method = init.method;
      const input = method === 'POST' ? JSON.parse(init.body) : undefined;
      const label = reader === 'foreign' ? 'foreign' : method === 'POST'
        ? (input.stream ? 'live' : 'answer') : reader === 'legacy' ? 'retained' : 'current';
      assert.ok(!captures.has(label), 'duplicate HTTP exchange');
      const capture = captureResponse(response, {reader, method, input, path: uri.pathname + uri.search});
      capture.catch(() => {}); // The bounded capture is awaited after the SDK stream.
      captures.set(label, capture);
      return response;
    }
  });
}
const c = await client(oldSdk,cfg.key,'legacy');
const modern = await client(newSdk,cfg.key,'current');
const foreign = await client(oldSdk,cfg.other,'foreign');
const common = {model:'qwen3.8-flash-next',store:true,temperature:0,reasoning:{effort:'none'},max_output_tokens:140};
const tool = {type:'function',name:'get_project_release',description:'Read the actual default q4t project publication revision.',
              parameters:{type:'object',properties:{},required:[],additionalProperties:false},strict:true};
const live = await c.responses.create({...common,input:'Call get_project_release to look up the current q4t project release.',
  tools:[tool],tool_choice:{type:'function',name:tool.name},stream:true,stream_options:{include_obfuscation:false}}).withResponse();
const vary = (live.response.headers.get('vary')??'').toLowerCase().split(',').map(v=>v.trim());
assert.ok(vary.includes('x-stainless-lang') && vary.includes('x-stainless-package-version'));
const frames = [];
for await (const event of live.data) frames.push(event);
assert.deepEqual(frames.map(e=>e.sequence_number),frames.map((_,i)=>i));
const terminal = frames.at(-1);
assert.equal(terminal.type,'response.completed');
const original = terminal.response;
assert.equal(original.prompt_cache_retention,'in-memory');
const calls = original.output.filter(i=>i.type==='function_call');
const marks = frames.filter(e=>e.type==='response.function_call_arguments.done');
assert.equal(calls.length,1);
assert.equal(marks.length,1);
const [fn]=calls, [mark]=marks;
assert.equal(fn.name,tool.name);
assert.equal(mark.name,fn.name);
assert.equal(original.output[mark.output_index].id,mark.item_id);
assert.equal(mark.item_id,fn.id);
assert.deepEqual(JSON.parse(mark.arguments),{});
assert.equal(mark.arguments,fn.arguments);
const cursor=mark.sequence_number-1;
const query={stream:true,starting_after:cursor,include_obfuscation:false};
const read=async api=>{
  const events=[];
  for await (const e of await api.responses.retrieve(original.id,query)) events.push(e);
  return events;
};
const retained=await read(c);
assert.deepEqual(retained,frames.filter(e=>e.sequence_number>cursor));
const current=await read(modern);
assert.deepEqual(current.map(e=>[e.type,e.sequence_number]),retained.map(e=>[e.type,e.sequence_number]));
assert.equal(current[0].item_id,fn.id);
assert.equal(current[0].arguments,mark.arguments);
assert.ok(!Object.hasOwn(current[0],'name'));
assert.equal(current.at(-1).response.prompt_cache_retention,'in_memory');
assert.equal(current.at(-1).response.id,original.id);
await assert.rejects(foreign.responses.retrieve(original.id),e=>e.status===404);
const receipt=JSON.parse(readFileSync(cfg.release_record,'utf8'));
assert.equal(receipt.status,'PASS');
assert.equal(receipt.publication_revision,cfg.expected_revision);
const revision=receipt.publication_revision;
const answer=await c.responses.create({...common,previous_response_id:original.id,tool_choice:'none',
  input:[{type:'function_call_output',call_id:fn.call_id,output:JSON.stringify({revision})}],
  instructions:'Reply with just the publication revision number returned by the tool.'});
assert.equal(answer.status,'completed');
assert.equal(answer.output_text.trim(),String(revision));
mkdirSync(cfg.capture,{mode:0o700});
for(const [label,payload] of Object.entries({live:frames,retained,current,answer})) {
  writeFileSync(resolve(cfg.capture,label+'.json'),JSON.stringify(payload)+'\n',{mode:0o600,flag:'wx'});
}
assert.deepEqual([...captures.keys()].sort(), ['answer','current','foreign','live','retained']);
const exchanges = Object.fromEntries(await Promise.all(
  [...captures].map(async ([label, packet]) => [label, await packet])));
writeFileSync(resolve(cfg.capture,'exchanges.json'),JSON.stringify(exchanges)+'\n',
              {mode:0o600,flag:'wx'});
console.log(JSON.stringify({sdk:version(oldSdk),peer:404,answer:answer.output_text.trim(),revision,response_id:original.id,
  followup:answer.id,function:fn.name,item_id:fn.id,call_id:fn.call_id,marker:mark,cursor,frames:frames.length,
  resumed:retained.length,modern:{sdk:version(newSdk),has_name:Object.hasOwn(current[0],'name'),same_item:true},vary}));
