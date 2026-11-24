# OpenAI SDK type-derived contract

The item, Realtime, Live, Decisions, Responses multi-agent, Managed Agents,
ChatKit session and thread, automation and invocation, Vault credential, and hosted environment type graphs in
`src/server/contracts/` are generated from the official OpenAI Node SDK
**7.47.0** declarations. Each graph records its source paths and SHA-256
digests. The SDK uses the Apache-2.0 license. [Original license](LICENSE)

q4t's extractor and native validator are original project code. TypeScript
**5.9.3** is a development-only compiler used by the extractor; it is not
linked, copied into the native service, or required to build the checked-in
contract. Runtime generation, tools, media and state semantics remain q4t's
own implementations; this declaration does not claim those APIs are complete.

The hosted template field sizes supplement the SDK types with facts extracted
from the frozen OpenAI HTTP specification. The extractor records the upstream
commit and content digest. [OpenAPI source](https://github.com/openai/openai-openapi/blob/926a7b8133226630bac025350e84b0ca5c31d45a/openapi.json)
