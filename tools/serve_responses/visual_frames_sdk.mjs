// Exercise the media parts that enter Chat and the video file adapter through HTTPS.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const piece of process.stdin) {
  input += piece;
  assert.ok(Buffer.byteLength(input) <= 16384);
}
const plan = JSON.parse(input);
const {OpenAI, version} = loadSdk(plan.sdk);
const client = key => new OpenAI({baseURL:plan.base, apiKey:key, maxRetries:0, timeout:90000});
const owner = client(plan.key);
const peer = client(plan.other);
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
function readFixture(spec) {
  const bytes = readFileSync(spec.path);
  assert.equal(sha(bytes), spec.sha256);
  return bytes.toString('base64');
}
const images = Object.fromEntries(plan.images.map(spec =>
  [spec.color, 'data:image/png;base64,' + readFixture(spec)]));
const frames = {video_frames:[images.green, images.green], video_timestamps:[0, 1]};
const question = 'What single solid color fills this video? Answer with one English color word.';
const guidance = 'For a visual observation, begin directly';
function chat(part) {
  return {model:plan.model, temperature:0, enable_thinking:false, max_completion_tokens:96,
    messages:[{role:'user',content:[part, {type:'text',text:question}]}]};
}
const preflight = async request => owner.post('/chat/preflight', {body:{request,include_prompt:true}});
const previews = [];
for (const part of [{type:'video_frames', ...frames}, frames, {image_url:images.blue}]) {
  const view = await preflight(chat(part));
  assert.equal(view.media.items, 1);
  assert.ok(view.media.visual_tokens > 0);
  previews.push({guidance:view.prompt.includes(guidance), prompt_sha256:sha(view.prompt),
    prompt_tokens:view.prompt_tokens, visual_tokens:view.media.visual_tokens});
}
assert.equal(previews[0].prompt_sha256, previews[1].prompt_sha256);
const malformed = chat({type:'video_url',video_url:{url:'data:video/mp4;base64,AQ=='}});
await assert.rejects(owner.chat.completions.create(malformed), error => error.status === 400);
const first = await owner.chat.completions.create(chat({type:'video_frames', ...frames}));
const green = first.choices[0].message;
let blue = '';
let streamedRefusal = '';
let finish = '';
const stream = await owner.chat.completions.create({...chat({image_url:images.blue}), stream:true});
for await (const chunk of stream) {
  const choice = chunk.choices[0];
  if (!choice) continue;
  blue += choice.delta.content ?? '';
  streamedRefusal += choice.delta.refusal ?? '';
  if (choice.finish_reason) finish = choice.finish_reason;
}
const video = await owner.responses.create({model:plan.model, temperature:0,
  reasoning:{effort:'none'}, max_output_tokens:96, input:[{role:'user',content:[
    {type:'input_file',filename:'clip.mp4',file_data:readFixture(plan.clip)},
    {type:'input_text',text:'What color fills the final frames of the video? Answer with one English color word.'}]}]});
const parts = x => x.output.filter(item => item.type === 'message').flatMap(item => item.content);
const clipParts = parts(video);
assert.deepEqual(parts(await owner.responses.retrieve(video.id)), clipParts);
await assert.rejects(peer.responses.retrieve(video.id), error => error.status === 404);
const independent = await owner.chat.completions.create({model:plan.model,temperature:0,
  enable_thinking:false, max_completion_tokens:32, messages:[{role:'user',content:'Compute 17 plus 25. Reply only with the number.'}]});
const word = (value, color) => typeof value === 'string' && new RegExp('^'+color+'[.!]?$', 'i').test(value.trim());
const answer = independent.choices[0].message;
const controls = {templates:previews.every(v => v.guidance),
  chat_video:first.choices[0].finish_reason === 'stop' && !green.refusal && word(green.content,'green'),
  chat_untyped_image:finish === 'stop' && !streamedRefusal && word(blue,'blue'),
  file_video:video.status === 'completed' && clipParts.length === 1 && clipParts[0].type === 'output_text' && word(clipParts[0].text,'blue'),
  independent:answer.content?.trim() === '42' && !answer.refusal};
process.stdout.write(JSON.stringify({sdk:version,previews,invalid_video_url:400,chat_video:green,
  chat_untyped_image:{text:blue,refusal:streamedRefusal,finish},file_video:{id:video.id,parts:clipParts,peer:404,sha256:plan.clip.sha256},
  independent:answer.content,controls,passed:Object.values(controls).every(Boolean)}));
