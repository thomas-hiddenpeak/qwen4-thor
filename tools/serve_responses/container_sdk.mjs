// Native TLS, two real tenants and an actual project publication receipt.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const bytes of process.stdin) {
  input += bytes;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
const client = apiKey => new OpenAI({apiKey, baseURL:base.href, maxRetries:0, timeout:60000});
const owner = client(plan.key), other = client(plan.other);
const api = owner.containers;
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const receipt = readFileSync(plan.source);
assert.equal(hash(receipt), plan.source_sha256);
const wire = [], negatives = [];
async function json(shape, promise) {
  const response = await promise.asResponse();
  assert.equal(response.status, 200);
  const value = await response.json();
  const method = shape.endsWith('created') ? 'POST' : shape.endsWith('deleted') ? 'DELETE' : 'GET';
  wire.push({shape,method,path:new URL(response.url).pathname,value});
  return value;
}
async function denied(status, promise) {
  await assert.rejects(promise, error => error.status === status);
  negatives.push(status);
}
const file = await owner.files.create({purpose:'assistants',file:new File([receipt],'release.json')});
const container = await json('created', api.create({name:'q4t-receipt',file_ids:[file.id],
  memory_limit:'4g', expires_after:{anchor:'last_active_at',minutes:5},
  network_policy:{type:'disabled'}}));
const id = container.id;
assert.equal(container.status,'running');
assert.equal(container.memory_limit,'4g');
assert.deepEqual(container.network_policy,{type:'disabled'});
const files = api.files;
const scoped = {container_id:id};
const initial = await json('file_list', files.list(id));
assert.equal(initial.data.length,1);
const original = initial.data[0];
assert.equal(original.path,'/mnt/data/release.json');
await denied(404,other.containers.retrieve(id));
await denied(404,other.containers.files.create(id,{file:new File(['isolated'],'other.txt')}));
await denied(404,other.containers.create({name:'invalid-copy',file_ids:[file.id]}));
const duplicate = await json('file_created',files.create(id,{file_id:file.id}));
assert.notEqual(original.path,duplicate.path);
const form = new FormData();
form.set('file_id',file.id);
const link = await json('file_created',owner.post('/containers/'+id+'/files',{body:form}));
assert.notEqual(link.id,duplicate.id);
await owner.files.delete(file.id);
const data = Buffer.alloc((1<<20)+197);
for (let i=0;i<data.length;i++) data[i]=i%251;
const blob = await json('file_created',files.create(id,{file:new File([data],'chunks.bin')}));
assert.equal(blob.bytes,data.length);
const streamed = await files.content.retrieve(blob.id,scoped);
assert.equal(Number(streamed.headers.get('content-length')),data.length);
const received = Buffer.from(await streamed.arrayBuffer());
assert.equal(hash(received),hash(data));
const persisted = await files.content.retrieve(original.id,scoped);
const retrieved = Buffer.from(await persisted.arrayBuffer());
assert.equal(hash(retrieved),plan.source_sha256);
const revision = JSON.parse(retrieved).publication.publication_revision;
assert.ok(Number.isSafeInteger(revision));
assert.deepEqual(await json('file_resource',files.retrieve(original.id,scoped)),original);
const page1 = await json('file_list',files.list(id,{order:'asc',limit:2}));
const page2 = await json('file_list',files.list(id,{order:'asc',limit:2,after:page1.last_id}));
assert.equal(page1.has_more,true);
assert.equal(page2.has_more,false);
assert.equal(new Set([...page1.data,...page2.data].map(f=>f.id)).size,4);
const row = await json('resource',api.retrieve(id));
assert.equal(row.id,id);
const allowed = Array.from({length:150}, (_, i) => 'release-'+i+'.project-documents.example');
const options = {name:'q4t-other',network_policy:{type:'allowlist',allowed_domains:allowed}};
assert.ok(Buffer.byteLength(JSON.stringify(options)) > 4096);
const second = await json('created',api.create(options));
assert.deepEqual(second.network_policy.allowed_domains,allowed);
const empty = await json('file_list',files.list(second.id));
assert.deepEqual([empty.data,empty.first_id,empty.last_id],[[],'','']);
await denied(404,files.retrieve(original.id,{container_id:second.id}));
await denied(404,other.containers.files.content.retrieve(original.id,scoped));
const named = await json('list',api.list({name:'q4t-receipt',limit:1}));
assert.deepEqual(named.data.map(x=>x.id),[id]);
const absent = await json('list',other.containers.list({name:'q4t-receipt'}));
assert.deepEqual([absent.data,absent.first_id,absent.last_id],[[],'','']);
for (const reversed of [false,true]) {
  const fields = new FormData();
  if (reversed) fields.set('file_id',file.id);
  fields.set('file',new File(['unused'],'unused.txt'));
  if (!reversed) fields.set('file_id',file.id);
  await denied(400,owner.post('/containers/'+id+'/files',{body:fields}));
}
assert.equal((await files.list(id)).data.length,4);
assert.deepEqual(await json('file_deleted',files.delete(blob.id,scoped)),
  {id:blob.id,object:'container.file.deleted',deleted:true});
await denied(404,files.content.retrieve(blob.id,scoped));
for (const key of [second.id,id])
  assert.deepEqual(await json('deleted',api.delete(key)),{id:key,object:'container.deleted',deleted:true});
await denied(404,api.retrieve(id));
const independent = await owner.chat.completions.create({model:'qwen3.8-flash-next',temperature:0,
  reasoning_effort:'none',max_tokens:16,messages:[{role:'user',content:'What is 6 times 7? Reply only with the number.'}]});
const answer = independent.choices[0].message.content.trim();
assert.equal(answer,'42');
process.stdout.write(JSON.stringify({sdk:version,wire,negatives,source_sha256:hash(retrieved),
  release_revision:revision,independent_chat:answer,chunked_file:{bytes:received.length,sha256:hash(received)}})+'\n');
