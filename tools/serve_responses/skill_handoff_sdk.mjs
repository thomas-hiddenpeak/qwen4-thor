// The light profile receives only public IDs and digests from the primary.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  assert.ok(Buffer.byteLength(input) <= 32768);
}
const p = JSON.parse(input);
const origin = new URL(p.base);
assert.equal(origin.protocol,'https:');
assert.equal(origin.hostname,'127.0.0.1');
const {OpenAI, version} = loadSdk(p.sdk);
const connect = apiKey => new OpenAI({apiKey,baseURL:p.base,maxRetries:0,timeout:60000});
const client = connect(p.key), other = connect(p.other);
const source = readFileSync(p.source);
const hash = b => createHash('sha256').update(b).digest('hex');
assert.equal(hash(source),p.source_sha256);
const revision = JSON.parse(source).publication.publication_revision;
const hidden = e => e instanceof OpenAI.APIError && e.status === 404;
const archive = async r => hash(Buffer.from(await (await r).arrayBuffer()));
let ids;
if (p.phase === 'create') {
  const parts = description => [new File([
    '---\nname: q4t-release\ndescription: '+description+'\n---\nRead release.json.\n'
  ],'q4t-release/SKILL.md'),new File([source],'q4t-release/release.json')];
  const skill = await client.skills.create({files:parts('Lookup the accepted release.')});
  const second = await client.skills.versions.create(skill.id,{files:parts('Read the updated manifest.'),default:true});
  const file = await client.files.create({purpose:'assistants',file:new File([source],'release.json')});
  const container = await client.containers.create({name:'release-handoff',file_ids:[file.id],
    skills:[{type:'skill_reference',skill_id:skill.id,version:'1'}]});
  const page = await client.containers.files.list(container.id);
  assert.equal(page.data.length,1);
  assert.equal(page.data[0].path,'/mnt/data/release.json');
  await client.files.delete(file.id);
  ids = {skill:skill.id, version_id:second.id, container:container.id,file:page.data[0].id};
} else {
  assert.equal(p.phase,'resume');
  ids = p.prior.ids;
}
const parent = {skill_id:ids.skill};
const skill = await client.skills.retrieve(ids.skill);
assert.equal(skill.default_version,'2');
assert.equal(skill.latest_version,'2');
const second = await client.skills.versions.retrieve('2',parent);
assert.equal(second.id,ids.version_id);
const v1 = await archive(client.skills.versions.content.retrieve('1',parent));
const v2 = await archive(client.skills.content.retrieve(ids.skill));
assert.notEqual(v1,v2);
const data = await client.containers.files.content.retrieve(ids.file,{container_id:ids.container});
const bytes = Buffer.from(await data.arrayBuffer());
assert.equal(hash(bytes),p.source_sha256);
assert.equal(JSON.parse(bytes).publication.publication_revision,revision);
await assert.rejects(other.skills.retrieve(ids.skill),hidden);
await assert.rejects(other.containers.files.content.retrieve(ids.file,{container_id:ids.container}),hidden);
if (p.phase === 'resume') {
  assert.deepEqual([v1,v2],[p.prior.v1,p.prior.v2]);
  assert.equal((await client.skills.delete(ids.skill)).deleted,true);
  const leftover = await client.containers.files.content.retrieve(ids.file,{container_id:ids.container});
  assert.equal(await archive(leftover),p.source_sha256);
  assert.equal((await client.containers.delete(ids.container)).deleted,true);
  await assert.rejects(client.containers.retrieve(ids.container),hidden);
}
process.stdout.write(JSON.stringify({phase:p.phase,sdk:version,ids,v1,v2,revision,
  source_sha256:hash(bytes),foreign_status:[404,404],deleted:p.phase==='resume'})+'\n');
