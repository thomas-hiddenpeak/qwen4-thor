// Compare native validation against the actual pinned SDK's TypeScript checker.
// Graph-derived candidates are not their own oracle: the SDK compiler decides
// assignability for every complete JSON literal, including negative mutations.
// A union's excess-property check can combine fields from different members.
// Require a concrete declared member too; retain both compiler observations.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
const [sdkDir, compilerDir, graphFile, outputDir] = process.argv.slice(2);
if (!outputDir || process.argv.length !== 6) throw new Error('SDK_DIR COMPILER_DIR GRAPH OUTPUT_DIR');
const sdk = realpathSync(resolve(sdkDir, 'node_modules/openai'));
const compiler = realpathSync(resolve(compilerDir, 'node_modules/typescript'));
const read = path => readFileSync(path, 'utf8');
const hash = path => createHash('sha256').update(readFileSync(path)).digest('hex');
if (JSON.parse(read(resolve(sdk, 'package.json'))).version !== '7.47.0' ||
    JSON.parse(read(resolve(compiler, 'package.json'))).version !== '5.9.3')
  throw new Error('pinned compiler/SDK identity mismatch');
const ts = createRequire(resolve(compiler, 'package.json'))(resolve(compiler, 'lib/typescript.js'));
const graph = JSON.parse(read(graphFile));
for (const [file, expected] of Object.entries(graph.sources_sha256))
  if (hash(resolve(sdk, file)) !== expected) throw new Error(`source drift: ${file}`);
function value(index, active = new Set()) {
  if (active.has(index)) throw new Error('recursive fixture requires explicit exemplar');
  const next = new Set(active); next.add(index);
  const node = graph.nodes[index];
  switch (node.kind) {
    case 'literal': return node.value;
    case 'null': return null;
    case 'string': return 'fixture';
    case 'number': return 1;
    case 'boolean': return false;
    case 'any': return { arbitrary: [17, null, { nested: true }] };
    case 'array': return [];
    case 'tuple': return node.elements.slice(0, node.required).map(x => value(x, next));
    case 'union': {
      for (const member of node.variants) {
        try { return value(member, next); } catch {}
      }
      throw new Error('no finite union exemplar');
    }
    case 'object': {
      const result = {};
      for (const field of node.fields)
        if (!field.optional || field.name === 'type') result[field.name] = value(field.type, next);
      if (node.additional !== null) result.dynamic_sdk_key = value(node.additional, next);
      return result;
    }
    default: throw new Error(`no exemplar for ${node.kind}`);
  }
}
const vectors = [];
for (const [shape, root] of Object.entries(graph.roots)) {
  for (const [i, member] of graph.nodes[root].variants.entries()) {
    const input = value(member);
    vectors.push({ name: `${shape}_${i}_valid`, shape, input, positive: true });
    vectors.push({ name: `${shape}_${i}_extra`, shape, input: { ...input, q4t_unknown_item_field: 1 } });
    vectors.push({ name: `${shape}_${i}_wrong_root`, shape, input: 'not-an-item' });
    for (const field of graph.nodes[member].fields ?? []) {
      if (!field.optional) {
        const without = { ...input }; delete without[field.name];
        vectors.push({ name: `${shape}_${i}_missing_${field.name}`, shape, input: without });
      }
    }
  }
}
// Independent, explicit shapes exercise optional/null/open-map boundaries.
vectors.push(
  { name: 'manual_pending_tool', shape: 'input_item', input: { type: 'function_call', call_id: 'call_1', name: 'lookup', arguments: '{' } },
  { name: 'manual_missing_tool_name', shape: 'input_item', input: { type: 'function_call', call_id: 'call_1', arguments: '{}' } },
  { name: 'manual_refusal', shape: 'conversation_item', input: { type: 'message', id: 'msg_1', role: 'assistant', status: 'completed', content: [{ type: 'refusal', refusal: 'No' }] } },
  { name: 'manual_image_original', shape: 'input_item', input: { type: 'message', role: 'user', content: [{ type: 'input_image', image_url: 'data:image/png;base64,AA==', detail: 'original' }] } },
  { name: 'manual_image_unknown_detail', shape: 'input_item', input: { type: 'message', role: 'user', content: [{ type: 'input_image', image_url: 'data:image/png;base64,AA==', detail: 'wrong' }] } },
  { name: 'manual_open_map', shape: 'input_item', input: { type: 'tool_search_output', call_id: 'call_1', tools: [{ type: 'function', name: 'lookup', parameters: { arbitrary: [1, { keep: true }] }, strict: false }] } },
);
const types = { input_item: 'ResponseInputItem', output_item: 'ResponseOutputItem', conversation_item: 'ConversationItem' };
const directory = resolve(outputDir); mkdirSync(directory, { recursive: true });
const fixture = resolve(directory, 'sdk-shape-cases.mts');
let source = `import type { ResponseInputItem, ResponseOutputItem } from ${JSON.stringify(resolve(sdk, 'src/resources/responses/responses.ts'))};\n`;
source += `import type { ConversationItem } from ${JSON.stringify(resolve(sdk, 'src/resources/conversations/items.ts'))};\n`;
const ranges = [];
for (const [i, item] of vectors.entries()) {
  const begin = source.length;
  source += `export const case${i}: ${types[item.shape]} = ${JSON.stringify(item.input)};\n`;
  ranges.push({ begin, end: source.length });
}
writeFileSync(fixture, source);
const program = ts.createProgram([fixture], {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.NodeNext,
  moduleResolution: ts.ModuleResolutionKind.NodeNext, strict: true,
  skipLibCheck: true, noEmit: true, allowImportingTsExtensions: true, types: [],
});
const diagnostics = ts.getPreEmitDiagnostics(program).filter(x => x.file?.fileName === fixture);
if (diagnostics.some(x => x.start < ranges[0].begin)) throw new Error('SDK import could not be checked');
for (let i = 0; i < vectors.length; ++i) {
  const errors = diagnostics.filter(d => d.start >= ranges[i].begin && d.start < ranges[i].end);
  vectors[i].union_accepted = errors.length === 0;
  vectors[i].diagnostics = errors.map(d => ({ code: d.code, message: ts.flattenDiagnosticMessageText(d.messageText, '\n') }));
  if (vectors[i].positive && !vectors[i].union_accepted)
    throw new Error(`generated graph contradicts SDK: ${vectors[i].name}: ${JSON.stringify(vectors[i].diagnostics)}`);
}
const checker = program.getTypeChecker();
const memberNames = {};
for (const [shape, name] of Object.entries(types)) {
  const file = resolve(sdk, shape === 'conversation_item'
    ? 'src/resources/conversations/items.ts' : 'src/resources/responses/responses.ts');
  const module = checker.getSymbolAtLocation(program.getSourceFile(file));
  const symbol = checker.getExportsOfModule(module).find(x => x.name === name);
  const members = checker.getDeclaredTypeOfSymbol(symbol).types;
  if (!members || members.length !== graph.nodes[graph.roots[shape]].variants.length)
    throw new Error(`SDK member count changed: ${shape}`);
  memberNames[shape] = members.map(member => {
    const symbol = member.aliasSymbol ?? member.symbol;
    const qualified = checker.getFullyQualifiedName(symbol);
    const match = /^"([^"]+)"\.(.+)$/.exec(qualified);
    const file = symbol.declarations[0].getSourceFile().fileName;
    if (!match || !file.startsWith(sdk + '/src/'))
      throw new Error(`cannot resolve concrete SDK type: ${qualified}`);
    return { name: match[2], type: `import(${JSON.stringify(file)}).${match[2]}` };
  });
}
const concreteFixture = resolve(directory, 'sdk-concrete-cases.mts');
let concreteSource = '';
const concreteRanges = [];
for (const [i, item] of vectors.entries()) {
  // A value rejected by the whole union cannot satisfy one of its members.
  if (!item.union_accepted) continue;
  for (const [j, member] of memberNames[item.shape].entries()) {
    const begin = concreteSource.length;
    concreteSource += `export const case${i}_${j}: ${member.type} = ${JSON.stringify(item.input)};\n`;
    concreteRanges.push({ begin, end: concreteSource.length, vector: i, name: member.name });
  }
}
writeFileSync(concreteFixture, concreteSource);
const concreteProgram = ts.createProgram([concreteFixture], program.getCompilerOptions());
const concreteDiagnostics = ts.getPreEmitDiagnostics(concreteProgram)
  .filter(x => x.file?.fileName === concreteFixture);
for (const item of vectors) item.concrete_members = [];
const rejectedMembers = new Map();
for (const range of concreteRanges) {
  const errors = concreteDiagnostics.filter(d => d.start >= range.begin && d.start < range.end);
  if (errors.length === 0) vectors[range.vector].concrete_members.push(range.name);
  else {
    if (!rejectedMembers.has(range.vector)) rejectedMembers.set(range.vector, []);
    rejectedMembers.get(range.vector).push({ member: range.name,
      diagnostics: errors.map(d => ({ code: d.code, message: ts.flattenDiagnosticMessageText(d.messageText, '\n') })) });
  }
}
for (const [i, item] of vectors.entries()) {
  item.accepted = item.concrete_members.length !== 0;
  if (item.positive && !item.accepted)
    throw new Error(`positive exemplar has no concrete SDK member: ${item.name}`);
  if (item.union_accepted && !item.accepted)
    item.concrete_rejections = rejectedMembers.get(i);
}
const result = { profile: 'q4t-sdk-item-shape-oracle-v1', sdk_version: '7.47.0',
  compiler_version: '5.9.3', validation_mode: 'concrete-sdk-union-members',
  graph_sha256: hash(graphFile), fixture_sha256: hash(fixture),
  concrete_fixture_sha256: hash(concreteFixture), member_names: memberNames,
  concrete_checks: concreteRanges.length, vectors };
writeFileSync(resolve(directory, 'vectors.json'), JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify({ total: vectors.length, accepted: vectors.filter(x => x.accepted).length,
  rejected: vectors.filter(x => !x.accepted).length,
  union_only: vectors.filter(x => x.union_accepted && !x.accepted).length,
  concrete_checks: concreteRanges.length, output: resolve(directory, 'vectors.json') }));
