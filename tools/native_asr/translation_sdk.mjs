// Exercise the official upload transport against the native TLS gateway.
import {readFileSync} from 'node:fs';
import {loadSdk} from '../serve_protocol/sdk.mjs';

let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  if (input.length > 8192) throw new Error('SDK input too large');
}
const options = JSON.parse(input);
const {OpenAI, version} = loadSdk(options.sdk_directory);
const endpoint = new URL(options.base_url);
if (endpoint.hostname !== '127.0.0.1' || endpoint.protocol !== 'https:' ||
    endpoint.pathname !== '/v1' || endpoint.username || endpoint.password)
  throw new Error('Expected a local TLS test endpoint');
const client = new OpenAI({baseURL: options.base_url, apiKey: options.key,
  maxRetries: 0, timeout: 45000});
const clip = readFileSync(options.wav);
const makeFile = () => new File([clip], 'speech.wav', {type:'audio/wav'});
const receipt = {sdk:version, formats:{}, negative:{}};
function check(condition, message) {
  if (!condition) throw new Error(message);
}
try {
  for (const [kind, extra] of Object.entries({
    unknown_model:{model:'absent-local-model'}, unknown_field:{language:'zh'},
    bad_format:{response_format:'diarized_json'}})) {
    let status;
    try {
      await client.audio.translations.create({model:'whisper-1',file:makeFile(),...extra});
    } catch (error) {status = error.status;}
    receipt.negative[kind] = status;
    check(status === (kind === 'unknown_model' ? 404 : 400), kind);
  }
  for (const format of ['json','text','verbose_json','srt','vtt']) {
    const model = format === 'text' ? 'qwen3-asr-0.6b' : 'whisper-1';
    const response = await client.audio.translations.create({model,file:makeFile(),
      response_format:format,temperature:0}).asResponse();
    const wire = await response.text();
    const type = response.headers.get('content-type');
    const row = {status:response.status,content_type:type,wire};
    receipt.formats[format] = row;
    check(response.status === 200 && wire.length > 0 && wire.length < 8192, format);
    let content = wire;
    if (format === 'json' || format === 'verbose_json') {
      check(type.startsWith('application/json'), 'JSON type');
      const parsed = JSON.parse(wire);
      content = parsed.text;
      if (format === 'verbose_json')
        check(parsed.language === 'english' && Math.abs(parsed.duration-options.duration)<.0011,
          'measured duration and English language');
    } else if (format === 'text') {
      check(type.startsWith('text/plain'), 'plain text type');
    } else {
      const vtt = format === 'vtt';
      check(type.startsWith(vtt ? 'text/vtt' : 'application/x-subrip') &&
        wire.startsWith(vtt ? 'WEBVTT\n\n' : '1\n'), 'subtitle framing');
      const pattern = /(\d\d):(\d\d):(\d\d)([.,])(\d{3}) --> (\d\d):(\d\d):(\d\d)([.,])(\d{3})/g;
      let end = 0, count = 0;
      for (const m of wire.matchAll(pattern)) {
        const ms = (h,min,s,frac) => (+h*3600 + +min*60 + +s)*1000 + +frac;
        const begin = ms(m[1],m[2],m[3],m[5]);
        const next = ms(m[6],m[7],m[8],m[10]);
        check(m[4] === (vtt ? '.' : ',') && m[9] === m[4] &&
          end <= begin && begin < next && next <= options.duration*1000+1, 'source cue timing');
        end = next;
        count++;
      }
      check(count>0,'timed English cues');
      content = wire.replace(pattern,'').replace(/^\d+$/gm,'').replace(/^WEBVTT$/m,'');
    }
    check(/\bproject(?:’s|'s)?\b/i.test(content) && /\b(version|release)\b/i.test(content) &&
      /\b(current|latest|present)\b/i.test(content) && !/\p{Script=Han}/u.test(content),
      format+' English meaning');
  }
  const chat = await client.chat.completions.create({model:'qwen3.8-flash-next',
    messages:[{role:'user',content:'What is 6 times 7? Reply only with the number.'}],
    temperature:0,reasoning_effort:'none',max_tokens:20});
  receipt.independent_chat = chat.choices[0].message.content.trim();
  check(receipt.independent_chat==='42','independent Chat');
  receipt.status='PASS';
} catch (error) {
  receipt.status='FAIL';
  receipt.error={type:error.name,code:error.code ?? null,status:error.status ?? null,
    message:String(error.message).slice(0,512)};
  process.exitCode=1;
}
process.stdout.write(JSON.stringify(receipt)+'\n');
