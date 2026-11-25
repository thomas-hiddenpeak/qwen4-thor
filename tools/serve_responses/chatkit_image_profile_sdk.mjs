// Reopen a real image thread and verify new file bytes survive the profile switch.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

const root = new URL('../../', import.meta.url);
const history = JSON.parse(readFileSync(new URL(
  'docs/evidence/serve-chatkit-image-attachments-20261126/history.json', root)));
const bytes = readFileSync(new URL('tests/fixtures/file_images/original.png', root));
const sha = value => createHash('sha256').update(value).digest('hex');
let input = '';
for await (const block of process.stdin) input += block;
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = apiKey => new OpenAI({apiKey, baseURL:plan.base, maxRetries:0});
const owner = client(plan.key), peer = client(plan.other);
const session = user => owner.beta.chatkit.sessions.create({user, workflow:{id:history.agent},
  chatkit_configuration:{file_upload:{enabled:true,max_files:2,max_file_size:1}}});
const grant = await session(history.user);
async function rpc(secret, type, params, expected=200) {
  const res = await fetch(plan.base + '/chatkit', {method:'POST',
    headers:{Authorization:'Bearer ' + secret, 'Content-Type':'application/json'},
    body:JSON.stringify({type,params}), signal:AbortSignal.timeout(15000)});
  assert.equal(res.status, expected, type);
  return res.json();
}
const query = {thread_id:history.thread,order:'asc'};
const page = await rpc(grant.client_secret, 'items.list', query);
const managed = await owner.beta.chatkit.threads.listItems(history.thread, {order:'asc'});
const ids = page.data.map(i => i.id);
assert.deepEqual(ids, history.items);
assert.deepEqual(managed.data.map(i => i.id), ids);
const attachment = page.data.find(i => i.id === history.item).attachments[0];
assert.deepEqual(attachment, managed.data.find(i => i.id === history.item).attachments[0]);
assert.equal(attachment.type,'image');
assert.equal(attachment.id,history.file);
assert.equal(attachment.mime_type,history.mime);
assert.match(attachment.preview_url,/^data:image\/png;base64,/);
const preview = Buffer.from(attachment.preview_url.split(',')[1], 'base64');
assert.equal(sha(preview),history.preview_sha256);
assert.equal(preview.length,history.preview_bytes);
const archive = {thread:history.thread,item:history.item,file:attachment.id,items:ids,preview_sha256:sha(preview)};
const missing = err => err.status === 404;
await assert.rejects(owner.files.retrieve(history.file),missing);
const sibling = await session('another-visual-reader');
await rpc(sibling.client_secret,'items.list',query,404);
await assert.rejects(peer.beta.chatkit.threads.retrieve(history.thread),missing);

async function sourceDigest(id) {
  return sha(Buffer.from(await (await owner.files.content(id)).arrayBuffer()));
}
let recovered;
if (plan.phase === 'light') {
  const file = plan.previous.upload.file;
  const digest = await sourceDigest(file);
  assert.equal(digest,history.fresh_source_sha256);
  await assert.rejects(peer.files.retrieve(file),missing);
  await owner.files.delete(file);
  await assert.rejects(owner.files.retrieve(file),missing);
  recovered = {file,source_sha256:digest,peer:404,after_delete:404};
} else {
  assert.equal(plan.phase,'primary');
}
const reserved = await rpc(grant.client_secret,'attachments.create',{
  name:'profile-scene.png',mime_type:'image/png',size:bytes.length});
assert.equal(reserved.type,'image');
const opts = {redirect:'error',signal:AbortSignal.timeout(15000)};
const put = reserved.upload_descriptor;
assert.equal(new URL(put.url).origin,new URL(plan.base).origin);
assert.ok(reserved.preview_url.startsWith(put.url+'/preview?ticket=ckpv_'));
assert.equal((await fetch(reserved.preview_url,opts)).status,404);
const sent = await fetch(put.url,{...opts,method:put.method,headers:put.headers,body:bytes});
assert.equal(sent.status,200);
await sent.body.cancel();
const res = await fetch(reserved.preview_url,opts);
assert.equal(res.status,200);
assert.equal(res.headers.get('content-type'),'image/png');
assert.match(res.headers.get('cache-control'),/no-store/);
const png = Buffer.from(await res.arrayBuffer());
assert.equal(png.subarray(0,8).toString('hex'),'89504e470d0a1a0a');
const digest = await sourceDigest(reserved.id);
assert.equal(digest,sha(bytes));
await assert.rejects(peer.files.retrieve(reserved.id),missing);
const upload = {file:reserved.id,source_sha256:digest,preview_sha256:sha(png),before:404,read:200,peer:404};
if (plan.phase === 'light') await owner.files.delete(reserved.id);
const chat = await peer.chat.completions.create({model:plan.model,temperature:0,max_tokens:32,
  messages:[{role:'user',content:'Compute 17 + 25. Return only the integer.'}],
  chat_template_kwargs:{enable_thinking:false}});
process.stdout.write(JSON.stringify({phase:plan.phase,sdk:version,archive,source_deleted:404,
  sibling:404,peer:404,upload,chat:chat.choices[0].message.content.trim(),...(recovered ? {recovered} : {})}));
