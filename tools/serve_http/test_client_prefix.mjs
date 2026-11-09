// Actual pinned SDK and Node HTTP transport against a CPU-only models endpoint.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { loadSdk } from '../serve_protocol/sdk.mjs';
import { ChatClient } from '../examples/js/chat_client.mjs';
const {OpenAI}=loadSdk(process.env.Q4T_NODE_SDK_DIR);

test('prefixed official SDK preserves the actual target and authorization',async()=>{
  const seen=[];
  const server=createServer((req,res)=>{
    seen.push({url:req.url,key:req.headers.authorization});
    const value=JSON.stringify({object:'list',data:[{id:'qwen3.8-flash-next',object:'model',created:0,owned_by:'q4t'}]});
    res.writeHead(200,{'content-type':'application/json','content-length':Buffer.byteLength(value)});res.end(value);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try{
    const origin=`http://127.0.0.1:${server.address().port}`;
    const api=new ChatClient(OpenAI,{baseURL:origin+'/llm/thor/v1/',apiKey:'prefix-key'});
    const value=await api.models();assert.equal(value.models.object,'list');
    assert.deepEqual(seen,[{url:'/llm/thor/v1/models',key:'Bearer prefix-key'}]);
    assert.equal(new ChatClient(OpenAI,{baseURL:origin+'/v1'}).baseURL,origin+'/v1');
  }finally{await new Promise(resolve=>server.close(resolve));}
});

test('prefix rejects URL normalization, encoded separators and foreign authority',()=>{
  for(const path of ['/x','/x//v1','/x/../v1','/x/./v1','/%2e/v1','/a%2Fb/v1',
    '/v1//','/x/v1?q=1','/x/v1#x','/'+'x'.repeat(513)+'/v1'])
    assert.throws(()=>new ChatClient(OpenAI,{baseURL:'http://example.test'+path}));
  for(const baseURL of ['http://user:secret@example.test/x/v1','ftp://example.test/x/v1'])
    assert.throws(()=>new ChatClient(OpenAI,{baseURL}));
});
