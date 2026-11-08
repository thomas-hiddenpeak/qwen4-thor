// Development-only extractor. Runtime consumes the generated native type graph;
// it does not load Node, TypeScript, the SDK, or a network schema service.
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { resolve, relative } from 'node:path';

const [sdkDirectory, compilerDirectory, output, mode] = process.argv.slice(2);
if (!sdkDirectory || !compilerDirectory || !output ||
    (mode !== undefined && mode !== '--api') || process.argv.length > 6)
  throw new Error('usage: node generate_item_types.mjs SDK_DIR TYPESCRIPT_DIR OUTPUT [--api]');
const sdk = realpathSync(resolve(sdkDirectory, 'node_modules/openai'));
const compiler = realpathSync(resolve(compilerDirectory, 'node_modules/typescript'));
const read = file => readFileSync(file, 'utf8');
const hash = file => createHash('sha256').update(readFileSync(file)).digest('hex');
if (JSON.parse(read(resolve(sdk, 'package.json'))).version !== '7.47.0')
  throw new Error('SDK version drift');
if (JSON.parse(read(resolve(compiler, 'package.json'))).version !== '5.9.3')
  throw new Error('TypeScript version drift');
const require = createRequire(resolve(compiler, 'package.json'));
const ts = require(resolve(compiler, 'lib/typescript.js'));
const source = resolve(sdk, 'src/resources/responses/responses.ts');
const conversation = resolve(sdk, 'src/resources/conversations/items.ts');
const conversations = resolve(sdk, 'src/resources/conversations/conversations.ts');
const inputItems = resolve(sdk, 'src/resources/responses/input-items.ts');
const program = ts.createProgram([source, conversation,
  ...(mode ? [conversations, inputItems] : [])], {
  target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.NodeNext,
  moduleResolution: ts.ModuleResolutionKind.NodeNext,
  strictNullChecks: true,
  skipLibCheck: true,
  noEmit: true,
  types: [],
});
const checker = program.getTypeChecker();
const ids = new Map(), nodes = [], sources = new Set();
function track(symbol) {
  for (const declaration of symbol?.declarations ?? []) {
    const path = realpathSync(declaration.getSourceFile().fileName);
    if (path.startsWith(sdk + '/')) sources.add(path);
  }
}
function encode(type) {
  if (ids.has(type)) return ids.get(type);
  if (nodes.length >= 4096) throw new Error('type graph limit');
  const id = nodes.length;
  ids.set(type, id);
  nodes.push(null);
  track(type.aliasSymbol);
  track(type.symbol);
  const flags = type.flags;
  const F = ts.TypeFlags;
  let node;
  if (flags & (F.Any | F.Unknown)) {
    if (type.intrinsicName === 'error')
      throw new Error(`unresolved SDK type: ${checker.typeToString(type)}`);
    node = { kind: 'any' };
  } else if (flags & F.Never) {
    node = { kind: 'never' };
  } else if (flags & F.Null) {
    node = { kind: 'null' };
  } else if (flags & F.StringLiteral) {
    node = { kind: 'literal', value: type.value };
  } else if (flags & F.NumberLiteral) {
    node = { kind: 'literal', value: type.value };
  } else if (flags & F.BooleanLiteral) {
    node = { kind: 'literal', value: type.intrinsicName === 'true' };
  } else if (flags & F.String) {
    node = { kind: 'string' };
  } else if (flags & F.Number) {
    node = { kind: 'number' };
  } else if (flags & F.Boolean) {
    node = { kind: 'boolean' };
  } else if (flags & F.Undefined) {
    node = { kind: 'never' }; // JSON has no undefined value; optional is separate.
  } else if (type.isIntersection() && type.types.some(x => x.flags & F.String) &&
    type.types.every(x => (x.flags & F.String) ||
      ((x.flags & F.Object) && !checker.getPropertiesOfType(x).length &&
       !checker.getIndexTypeOfType(x, ts.IndexKind.String) &&
       !checker.getSignaturesOfType(x, ts.SignatureKind.Call).length))) {
    // SDK open string enums use string & {}. Do not traverse String.prototype
    // methods as JSON properties, or erase genuine object brand constraints.
    node = { kind: 'string' };
  } else if (type.isUnion()) {
    node = { kind: 'union', variants: type.types.filter(x => !(x.flags & F.Undefined)).map(encode) };
  } else if (checker.isTupleType(type)) {
    const elements = checker.getTypeArguments(type);
    const flags = type.target.elementFlags;
    if (flags.some(x => x & (ts.ElementFlags.Rest | ts.ElementFlags.Variadic)))
      throw new Error('variadic tuple requires explicit implementation');
    node = { kind: 'tuple', elements: elements.map(encode),
      required: flags.filter(x => x & ts.ElementFlags.Required).length };
  } else if (checker.isArrayType(type)) {
    const [element] = checker.getTypeArguments(type);
    if (!element) throw new Error('array element unresolved');
    node = { kind: 'array', element: encode(element) };
  } else if ((flags & F.Object) || type.isIntersection()) {
    const fields = [];
    for (const property of checker.getPropertiesOfType(type).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
      track(property);
      const declaration = property.valueDeclaration ?? property.declarations?.[0];
      if (!declaration) throw new Error(`missing property declaration ${property.name}`);
      fields.push({ name: property.name,
        optional: Boolean(property.flags & ts.SymbolFlags.Optional),
        type: encode(checker.getTypeOfSymbolAtLocation(property, declaration)) });
    }
    const index = checker.getIndexTypeOfType(type, ts.IndexKind.String);
    if (checker.getSignaturesOfType(type, ts.SignatureKind.Call).length)
      throw new Error(`callable type is not JSON data: ${checker.typeToString(type)}`);
    node = { kind: 'object', fields, additional: index ? encode(index) : null };
  } else {
    throw new Error(`unhandled type ${flags}: ${checker.typeToString(type)}`);
  }
  nodes[id] = node;
  return id;
}
function exported(file, name) {
  const module = checker.getSymbolAtLocation(program.getSourceFile(file));
  const symbol = checker.getExportsOfModule(module).find(x => x.name === name);
  if (!symbol) throw new Error(`missing export ${name}`);
  track(symbol);
  return encode(checker.getDeclaredTypeOfSymbol(symbol));
}
const roots = {
  input_item: exported(source, 'ResponseInputItem'),
  output_item: exported(source, 'ResponseOutputItem'),
  conversation_item: exported(conversation, 'ConversationItem'),
};
if (mode) Object.assign(roots, {
  response_create: exported(source, 'ResponseCreateParamsBase'),
  response: exported(source, 'Response'),
  response_event: exported(source, 'ResponseStreamEvent'),
  response_retrieve: exported(source, 'ResponseRetrieveParamsBase'),
  input_items_list: exported(inputItems, 'ResponseItemList'),
  input_items_query: exported(inputItems, 'InputItemListParams'),
  conversation: exported(conversations, 'Conversation'),
  conversation_create: exported(conversations, 'ConversationCreateParams'),
  conversation_update: exported(conversations, 'ConversationUpdateParams'),
  conversation_deleted: exported(conversations, 'ConversationDeletedResource'),
  conversation_items: exported(conversation, 'ConversationItemList'),
  conversation_items_create: exported(conversation, 'ItemCreateParams'),
  conversation_items_query: exported(conversation, 'ItemListParams'),
});
const result = {
  profile: mode ? 'q4t-openai-application-types-v1' : 'q4t-openai-item-types-v1',
  sdk_version: '7.47.0',
  compiler_version: '5.9.3', roots, nodes,
  sources_sha256: Object.fromEntries([...sources].sort().map(path => [relative(sdk, path), hash(path)])),
  license_sha256: hash(resolve(sdk, 'LICENSE')),
  compiler_sha256: hash(resolve(compiler, 'lib/typescript.js')),
};
writeFileSync(resolve(output), JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify({ output: resolve(output), nodes: nodes.length,
  sources: sources.size, roots, sha256: hash(resolve(output)) }));
