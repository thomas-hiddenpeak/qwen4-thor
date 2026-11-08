# Responses item declaration differential

`sdk_vectors.json` records 647 positive/negative examples checked by the actual
OpenAI Node SDK 7.47.0 declarations and TypeScript 5.9.3. It covers all 33 input,
28 output and 29 conversation union members. The graph proposes examples; the
independent compiler evaluates complete JSON literals against SDK declarations.

TypeScript accepts 13 malformed examples at the outer union level because its
excess-property check combines fields from separate members. None satisfies a
concrete declared member. The fixture preserves `union_accepted`, diagnostics,
and all concrete rejection diagnostics. The expected `accepted` value requires
a real concrete member; it is not edited to match the C++ result. 3,271 concrete
assignments establish 94 valid and 553 invalid examples. The original failing
634/647 comparison remains in the evidence archive.

This validates JSON type shape, not all API semantics: limits, ownership, tool
correlation, execution, storage and generated content require their own checks.
An incomplete tool history can be stored; generating from that history still
requires the corresponding generation contract.

Regenerate with the development-only tools (outputs belong in `.q4t-work/`):

```bash
node tools/serve_responses/generate_item_types.mjs SDK_DIR TS_DIR GRAPH.json
node tools/serve_responses/item_shape_vectors.mjs SDK_DIR TS_DIR GRAPH.json OUT
```

The arguments identify roots containing `node_modules/openai` and
`node_modules/typescript`. Both versions are checked, as are source digests.
The service consumes a checked-in graph embedded at build time; Node, the SDK
and TypeScript are not runtime dependencies. The SDK's Apache-2.0 license is
preserved under `third_party/licenses/openai-node/`.
