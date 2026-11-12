# Native Responses acceptance and declarations

These development tools do not add runtime dependencies. The native engine
handles Responses and Conversations directly. The pinned OpenAI Node SDK 7.47.0
and TypeScript 5.9.3 inspect and validate the public wire contract.

Regenerate the application type graph in a private workspace, then publish its
reviewed schema and OpenAPI (the source SDK and compiler remain read-only):

```sh
node tools/serve_responses/generate_item_types.mjs \
  .q4t-work/serve-protocol-20261103/sdk \
  .q4t-work/serve-responses-20261108/schema-tool \
  .q4t-work/serve-responses-20261108/application-types.json --api
python3 -B tools/serve_responses/build_contract.py \
  --types .q4t-work/serve-responses-20261108/application-types.json
python3 -B tools/serve_protocol/build_contract.py
```

Both publishers accept `--check`. The graph includes standard SDK input, output,
and event shapes; a structurally valid request still requires its real executor.
Pending executors remain implementation tasks in the master plan. The schema
records reviewed wire adaptations, including SDK-only `output_text`, native error
codes, and official HTTP `usage:null` on active/cancelled responses. A successful
terminal requires actual usage.

`test_contract.py` keeps historical nonconforming wire as negative evidence and
validates new JSON/SSE without filling missing fields. Set
`Q4T_CURRENT_APPLICATION_WIRE` to colon-separated native capture directories and
`Q4T_TEST_OUTPUT_DIR` to a private output directory. `test_background_sdk.mjs`
uses CPU fixtures and kills only its own fixture for recovery. Real model
acceptance must run under the repository's exclusive GPU lease.

Release declarations v3 carry the application schema and a self-contained OpenAPI
document. All packaged OpenAPI references are internal; verification does not
fetch a schema service or need this source tree. SDK-derived type notices remain
in `third_party/licenses/openai-node` and travel with the package.
