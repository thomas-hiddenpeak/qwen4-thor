// Official SDK over native TLS. Keep each observed JSON response for differential
// wire validation and save the downloaded directory ZIP for member verification.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync, writeFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const data of process.stdin) {
  input += data;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const base = new URL(plan.base);
assert.equal(base.protocol, 'https:');
assert.equal(base.hostname, '127.0.0.1');
const {OpenAI, version} = loadSdk(plan.sdk);
const connect = apiKey => new OpenAI({apiKey, baseURL:base.href, maxRetries:0, timeout:60000});
const client = connect(plan.key), other = connect(plan.other);
const api = client.skills;
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const receipt = readFileSync(plan.source), archive = readFileSync(plan.zip);
assert.equal(hash(receipt),plan.source_sha256);
assert.equal(hash(archive),plan.zip_sha256);
assert.ok(archive.length > 1 << 20);
const wire = [], negatives = [];
async function json(shape, method, promise) {
  const response = await promise.asResponse();
  assert.equal(response.status,200);
  const value = await response.json();
  wire.push({shape,method,path:new URL(response.url).pathname,value});
  return value;
}
async function denied(status, promise) {
  await assert.rejects(promise, error => error.status === status);
  negatives.push(status);
}
const dir = 'release-check';
const markdown = '---\nname: release-check\ndescription: Read the accepted project release.\n---\nRead release.json.\n';
const first = await json('resource','POST',api.create({files:[
  new File([markdown], dir+'/SKILL.md'), new File([receipt],dir+'/release.json')
]}));
assert.equal(first.name,dir);
assert.deepEqual([first.default_version,first.latest_version],['1','1']);
const id = first.id, parent = {skill_id:id};
const source = await api.content.retrieve(id);
assert.equal(source.headers.get('content-type'),'application/zip');
const packed = Buffer.from(await source.arrayBuffer());
writeFileSync(plan.download,packed,{flag:'wx',mode:0o600});
const second = await json('version_resource','POST',api.versions.create(id,{
  files:new File([archive],'version-two.zip'),default:false
}));
assert.equal(second.version,'2');
const before = await json('resource','GET',api.retrieve(id));
assert.deepEqual(before, {...first, latest_version:'2'});
const list = await json('list','GET',api.list({limit:0}));
assert.deepEqual([list.data,list.first_id,list.last_id,list.has_more],[[],null,null,true]);
const all = await json('list','GET',api.list({limit:2}));
assert.equal(all.data[0].id,id);
const page = await json('version_list','GET',api.versions.list(id,{order:'asc',limit:1}));
assert.equal(page.data[0].version,'1');
assert.equal(page.has_more,true);
const next = await json('version_list','GET',api.versions.list(id,{order:'asc',limit:1,after:page.last_id}));
assert.equal(next.data[0].id,second.id);
assert.equal(next.has_more,false);
assert.deepEqual(await json('version_resource','GET',api.versions.retrieve('2',parent)),second);
await denied(400,api.versions.delete('1',parent));
await denied(400,api.versions.retrieve('0',parent));
await denied(404,api.list({after:'skill_missing'}));
await denied(404,api.versions.list(id,{after:'skillver_missing'}));
await denied(404,other.skills.retrieve(id));
await denied(404,other.skills.versions.content.retrieve('2',parent));
await denied(404,other.containers.create({name:'foreign-skill',skills:[{type:'skill_reference',skill_id:id}]}));
const changed = await json('resource','POST',api.update(id,{default_version:'2'}));
assert.equal(changed.description,second.description);
assert.equal(changed.default_version,'2');
const downloaded = await api.content.retrieve(id);
const bytes = Buffer.from(await downloaded.arrayBuffer());
assert.equal(hash(bytes),hash(archive));
const jsonBody = await api.versions.content.retrieve('2',parent,{headers:{
  Accept:'application/json;q=0.9, application/zip;q=0.4'
}});
assert.equal(jsonBody.headers.get('content-type'),'application/json');
const text = await jsonBody.json();
assert.equal(text,archive.toString('base64'));
const excluded = await api.content.retrieve(id,{headers:{Accept:'*/*, application/zip;q=0'}});
assert.equal(await excluded.json(),text);
await denied(406,api.content.retrieve(id,{headers:{Accept:'text/plain'}}));
const mount = await client.containers.create({name:'accepted-skill-files',skills:[
  {type:'skill_reference',skill_id:id,version:'1'}, {type:'skill_reference',skill_id:id}
]});
const removed = await json('version_deleted','DELETE',api.versions.delete('1',parent));
assert.equal(removed.version,'1');
await denied(404,api.versions.retrieve('1',parent));
const gone = await json('deleted','DELETE',api.delete(id));
assert.equal(gone.deleted,true);
await denied(404,api.retrieve(id));
assert.equal((await client.containers.retrieve(mount.id)).id,mount.id);
await client.containers.delete(mount.id);
const independent = await client.chat.completions.create({model:'qwen3.8-flash-next',temperature:0,
  reasoning_effort:'none',max_tokens:16,messages:[{role:'user',content:'What is 6 times 7? Reply only with the number.'}]});
const answer = independent.choices[0].message.content.trim();
assert.equal(answer,'42');
process.stdout.write(JSON.stringify({sdk:version,wire,negatives,source_sha256:hash(receipt),
  release_revision:JSON.parse(receipt).publication.publication_revision, independent_chat:answer,
  directory_zip_sha256:hash(packed), large_zip:{bytes:bytes.length,sha256:hash(bytes)},
  base64_bytes:text.length, skill:id, container:mount.id})+'\n');
