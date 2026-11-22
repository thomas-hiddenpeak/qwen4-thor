// One authenticated SDK owner checks typed decisions and shared project input.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';
const check = (ok, message) => { if (!ok) throw new Error(message); };
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  check(Buffer.byteLength(input) <= 65536, 'bounded test arguments');
}
const args = JSON.parse(input);
const sdk = path.resolve(process.env.Q4T_NODE_SDK_DIR, 'node_modules/openai');
check(JSON.parse(fs.readFileSync(path.join(sdk,'package.json'))).version === '7.47.0', 'SDK version drift');
const {default: OpenAI} = await import(pathToFileURL(path.join(sdk,'index.mjs')));
const endpoint = new URL(args.base_url);
check(endpoint.protocol==='https:' && endpoint.hostname==='127.0.0.1' && endpoint.pathname==='/v1',
      'expected loopback TLS ingress');
const client = new OpenAI({baseURL:endpoint.toString(),apiKey:args.api_key,maxRetries:0,timeout:90000});
function record(spec) {
  const bytes=fs.readFileSync(spec.path);
  check(bytes.length<1<<20 && crypto.createHash('sha256').update(bytes).digest('hex')===spec.sha256,'project record changed');
  return bytes.toString();
}
const evidence = JSON.parse(record(args.evidence));
const projectText = Array.from(record(args.status)).slice(0,2500).join('');
const version=evidence.observation.version;
const model='qwen3.8-flash-next';
const results={status:'FAIL',attempted:[],responses:{}};
async function run(name, body) {
  results.attempted.push(name);
  return client.decisions.create({model,...body});
}
const asksVersion = {type:'predicate',name:'matches_record',
  instructions:'The project evidence reports the default version as '+version+'.'};
const choice = {type:'choice',name:'version',
  instructions:'Select the default version reported in the evidence.',
  choices:[{value:'rev000'},{value:version},{value:'unknown'}]};
const score = {type:'score',name:'agreement',instructions:'Rate how well the reported backend reply matches the reported default version.',
  levels:[{label:'different',description:'Contradictory values'},{label:'unclear',description:'Missing version'},
          {label:'match',description:'Both version strings are identical'}]};
try {
  try {await run('invalid',{input:'project',questions:[{...choice,choices:[{value:true},{value:true}]}]});}
  catch (e) {results.invalid={http:e.status,code:e.code};}
  check(results.invalid?.http===400,'duplicate typed choice not rejected');
  results.responses.project=await run('project',{input:[{role:'user',content:[{type:'input_json',
    data:{source:'project acceptance record',default_version:version,backend_reply:evidence.observation.backend}}]}],
    questions:[asksVersion,choice,score]});
  const shared={input:projectText,prompt_cache_key:'decision-release-review'};
  results.responses.write=await run('write',{...shared,questions:[{
    type:'predicate',instructions:'This project implements a native LLM inference service.'}]});
  results.responses.read=await run('read',{...shared,questions:[{
    type:'predicate',instructions:'This project builds a garden irrigation controller.'}]});
  results.status='PASS';
} catch (e) {
  results.error={http:e.status??null,code:e.code??null,message:String(e.message).slice(0,1024)};
  process.exitCode=2;
} finally {
  process.stdout.write(JSON.stringify(results)+'\n');
}
