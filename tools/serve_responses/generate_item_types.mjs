// Development-only extractor. Runtime consumes the generated native type graph;
// it does not load Node, TypeScript, the SDK, or a network schema service.
import { addTemplateBounds } from './environment_bounds.mjs';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { resolve, relative } from 'node:path';

const [sdkDirectory, compilerDirectory, output, mode] = process.argv.slice(2);
if (!sdkDirectory || !compilerDirectory || !output ||
    (mode !== undefined && !['--api', '--realtime', '--live', '--decisions', '--multi-agent', '--agents', '--assistants', '--agent-sessions', '--vault-credentials', '--environments'].includes(mode)) ||
    process.argv.length > 6)
  throw new Error('usage: node generate_item_types.mjs SDK_DIR TYPESCRIPT_DIR OUTPUT [--api|--realtime|--live|--decisions|--multi-agent|--agents|--assistants|--agent-sessions|--vault-credentials|--environments]');
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
const inputTokens = resolve(sdk, 'src/resources/responses/input-tokens.ts');
const realtime = resolve(sdk, 'src/resources/realtime/realtime.ts');
const realtimeCalls = resolve(sdk, 'src/resources/realtime/calls.ts');
const realtimeSecrets = resolve(sdk, 'src/resources/realtime/client-secrets.ts');
const live = resolve(sdk, 'src/resources/live/live.ts');
const decisions = resolve(sdk, 'src/resources/decisions.ts');
const threads = resolve(sdk, 'src/resources/beta/threads/threads.ts');
const messages = resolve(sdk, 'src/resources/beta/threads/messages.ts');
const assistants = resolve(sdk, 'src/resources/beta/assistants.ts');
const agents = resolve(sdk, 'src/resources/beta/agents/agents.ts');
const credentials = resolve(sdk, 'src/resources/beta/agents/vaults/credentials.ts');
const environments = resolve(sdk, 'src/resources/beta/agents/environments/environments.ts');
const environmentFiles = resolve(sdk, 'src/resources/beta/agents/environments/files.ts');
const templates = resolve(sdk, 'src/resources/beta/agents/environments/templates.ts');
const agentSessions = resolve(sdk, 'src/resources/beta/agents/sessions/sessions.ts');
const artifacts = resolve(sdk, 'src/resources/beta/agents/sessions/artifacts.ts');
const subagents = resolve(sdk, 'src/resources/beta/agents/sessions/subagents/subagents.ts');
const agentTurns = resolve(sdk, 'src/resources/beta/agents/sessions/turns/turns.ts');
const beta = resolve(sdk, 'src/resources/beta/responses/responses.ts');
const liveSessions = resolve(sdk, 'src/resources/live/sessions.ts');
const liveSideband = resolve(sdk, 'src/resources/live/sideband/sideband.ts');
const liveForks = resolve(sdk, 'src/resources/live/forks/forks.ts');
const program = ts.createProgram([source, conversation,
  ...(mode === '--api' ? [conversations, inputItems, inputTokens] : []),
  ...(mode === '--realtime' ? [realtime, realtimeCalls, realtimeSecrets] : []),
  ...(mode === '--decisions' ? [decisions] : []),
  ...(mode === '--multi-agent' ? [beta] : []),
  ...(mode === '--agents' ? [agents] : []),
  ...(mode === '--assistants' ? [assistants, threads, messages] : []),
  ...(mode === '--vault-credentials' ? [credentials] : []),
  ...(mode === '--environments' ? [environments, environmentFiles, templates] : []),
  ...(mode === '--agent-sessions' ? [agents, agentSessions, agentTurns, artifacts, subagents] : []),
  ...(mode === '--live' ? [live, liveSessions, liveSideband, liveForks] : [])], {
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
      // TypeScript synthesizes properties for mapped unions without a declaration.
      const propType = declaration ? checker.getTypeOfSymbolAtLocation(property, declaration) :
        checker.getTypeOfPropertyOfType(type, property.name);
      if (!propType) throw new Error(`unresolved property ${property.name}`);
      fields.push({ name: property.name,
        optional: Boolean(property.flags & ts.SymbolFlags.Optional),
        type: encode(propType) });
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
  let symbol = checker.getSymbolAtLocation(program.getSourceFile(file));
  for (const part of name.split('.')) {
    symbol = checker.getExportsOfModule(symbol).find(x => x.name === part);
    if (!symbol) throw new Error(`missing export ${name}`);
    track(symbol);
  }
  return encode(checker.getDeclaredTypeOfSymbol(symbol));
}
const roots = mode === '--environments' ? {
  template_create: exported(templates, 'TemplateCreateParams'),
  template_update: exported(templates, 'TemplateUpdateParams'),
  template_resource: exported(templates, 'EnvironmentTemplate'),
  template_deleted: exported(templates, 'EnvironmentTemplateDeleted'),
  template_query: exported(templates, 'TemplateListParams'),
  environment_create: exported(environments, 'EnvironmentCreateParams'),
  environment_resource: exported(environments, 'EnvironmentInfo'),
  environment_query: exported(environments, 'EnvironmentListParams'),
  file_create: exported(environmentFiles, 'FileCreateParams'),
  file_resource: exported(environmentFiles, 'EnvironmentFile'),
  file_query: exported(environmentFiles, 'FileListParams'),
} : mode === '--vault-credentials' ? {
  create: exported(credentials, 'CredentialCreateParams'),
  auth_create: exported(credentials, 'CredentialAuthCreateParam'),
  auth_rotate: exported(credentials, 'CredentialAuthRotateParam'),
  resource: exported(credentials, 'Credential'),
  deleted: exported(credentials, 'CredentialDeleted'),
} : mode === '--agent-sessions' ? {
  create: exported(agentSessions, 'SessionCreateParamsBase'),
  update: exported(agentSessions, 'SessionUpdateParams'),
  resource: exported(agents, 'AgentSession'),
  deleted: exported(agents, 'AgentSessionDeleted'),
  query: exported(agentSessions, 'SessionListParams'),
  input_event: exported(agents, 'AgentSessionInputParam'),
  event: exported(agents, 'AgentSessionEvent'),
  item: exported(agents, 'AgentSessionItem'),
  turn: exported(agentTurns, 'Turn'),
  artifact: exported(artifacts, 'SessionArtifact'),
  artifact_deleted: exported(artifacts, 'SessionArtifactDeleted'),
  artifact_query: exported(artifacts, 'ArtifactListParams'),
  subagent: exported(agents, 'Subagent'),
  subagent_query: exported(subagents, 'SubagentListParams'),
} : mode === '--assistants' ? {
  create: exported(assistants, 'AssistantCreateParams'),
  update: exported(assistants, 'AssistantUpdateParams'),
  resource: exported(assistants, 'Assistant'),
  deleted: exported(assistants, 'AssistantDeleted'),
  query: exported(assistants, 'AssistantListParams'),
  thread_create: exported(threads, 'ThreadCreateParams'),
  thread_update: exported(threads, 'ThreadUpdateParams'),
  thread_resource: exported(threads, 'Thread'),
  thread_deleted: exported(threads, 'ThreadDeleted'),
  message_create: exported(messages, 'MessageCreateParams'),
  message_update: exported(messages, 'MessageUpdateParams'),
  message_resource: exported(messages, 'Message'),
  message_deleted: exported(messages, 'MessageDeleted'),
  message_query: exported(messages, 'MessageListParams'),
} : mode === '--agents' ? {
  create: exported(agents, 'AgentCreateParams'),
  update: exported(agents, 'AgentUpdateParams'),
  resource: exported(agents, 'Agent'),
  deleted: exported(agents, 'AgentDeleted'),
} : mode === '--multi-agent' ? {
  configuration: exported(beta, 'ResponseCreateParams.MultiAgent'),
  input_call: exported(beta, 'BetaResponseInputItem.MultiAgentCall'),
  input_result: exported(beta, 'BetaResponseInputItem.MultiAgentCallOutput'),
  output_call: exported(beta, 'BetaResponseOutputItem.MultiAgentCall'),
  output_result: exported(beta, 'BetaResponseOutputItem.MultiAgentCallOutput'),
  output_message: exported(beta, 'BetaResponseOutputMessage'),
} : mode === '--decisions' ? {
  request: exported(decisions, 'DecisionCreateParams'),
  decision: exported(decisions, 'Decision'),
} : mode === '--live' ? {
  client_event: exported(live, 'ClientEvent'),
  server_event: exported(live, 'ServerEvent'),
  session_start: exported(live, 'SessionConfig'),
  session_update: exported(live, 'SessionUpdateConfig'),
  session: exported(live, 'SessionResource'),
  media_create: exported(live, 'LiveCreateParams'),
  media_created: exported(live, 'LiveCreateResponse'),
  sideband_client: exported(liveSideband, 'ConnectClientEvent'),
  sideband_server: exported(liveSideband, 'ConnectServerEvent'),
  fork_client: exported(liveForks, 'ForkClientEvent'),
  fork_server: exported(liveForks, 'ForkServerEvent'),
  session_accept: exported(liveSessions, 'SessionAcceptParams'),
  session_fork: exported(liveSessions, 'SessionForkParams'),
  session_forked: exported(liveSessions, 'SessionForkResponse'),
  session_refer: exported(liveSessions, 'SessionReferParams'),
  session_reject: exported(liveSessions, 'SessionRejectParams'),
} : mode === '--realtime' ? {
  conversation_item: exported(realtime, 'ConversationItem'),
  client_event: exported(realtime, 'RealtimeClientEvent'),
  server_event: exported(realtime, 'RealtimeServerEvent'),
  response_create: exported(realtime,
    'RealtimeResponseCreateParamsWithReferencesParam'),
  session: exported(realtime, 'RealtimeSessionCreateRequest'),
  response: exported(realtime, 'RealtimeResponse'),
  call_accept: exported(realtimeCalls, 'CallAcceptParams'),
  call_refer: exported(realtimeCalls, 'CallReferParams'),
  call_reject: exported(realtimeCalls, 'CallRejectParams'),
  client_secret_create: exported(realtimeSecrets, 'ClientSecretCreateParams'),
  client_secret_created: exported(realtimeSecrets, 'ClientSecretCreateResponse'),
} : {
  input_item: exported(source, 'ResponseInputItem'),
  output_item: exported(source, 'ResponseOutputItem'),
  conversation_item: exported(conversation, 'ConversationItem'),
};
if (mode === '--api') Object.assign(roots, {
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
  response_compact: exported(source, 'ResponseCompactParams'),
  compacted_response: exported(source, 'CompactedResponse'),
  input_tokens_request: exported(inputTokens, 'InputTokenCountParams'),
  input_tokens_response: exported(inputTokens, 'InputTokenCountResponse'),
});
if (mode === '--assistants') {
  // TypeScript numbers and strings do not encode the HTTP request bounds.
  const bounds = { name: {maxLength:256}, description: {maxLength:512},
    instructions: {maxLength:256000}, tools: {maxItems:128},
    temperature: {minimum:0, maximum:2}, top_p: {minimum:0, maximum:1},
    metadata: {maxProperties:16, propertyNames:{maxLength:64},
               additionalProperties:{maxLength:512}} };
  // The scoped SDK graph is for this resource family only. Array restrictions
  // belong to the field, never to a shared primitive or arbitrary tool schema.
  function codeFileLimit(index) {
    const node = nodes[index];
    if (node.kind === 'union') {
      for (const variant of node.variants) codeFileLimit(variant);
    }
    const files = node.fields?.find(field => field.name === 'file_ids');
    if (files) files.bounds = {maxItems:20};
  }
  for (const node of nodes) {
    for (const field of node.fields ?? []) {
      if (field.name === 'vector_stores' || field.name === 'vector_store_ids')
        field.bounds = {maxItems:1};
      if (field.name === 'code_interpreter') codeFileLimit(field.type);
    }
  }
  // MessageUpdateParams includes the parent path parameter on SDK calls.
  const update = nodes[roots.message_update];
  update.fields = update.fields.filter(field => field.name !== 'thread_id');
  function requireAttachment(index) {
    const node = nodes[index];
    if (node.kind === 'union') node.variants.forEach(requireAttachment);
    if (node.kind === 'array') {
      for (const field of nodes[node.element].fields) {
        if (field.name === 'file_id' || field.name === 'tools') field.optional = false;
      }
    }
  }
  for (const node of nodes.slice(roots.thread_create)) {
    const fields = node.fields ?? [];
    if (fields.some(field => field.name === 'role') &&
        !fields.some(field => field.name === 'id')) {
      fields.find(field => field.name === 'content').bounds = {minItems:1};
      requireAttachment(fields.find(field => field.name === 'attachments').type);
    }
    const metadata = fields.find(field => field.name === 'metadata');
    if (metadata) metadata.bounds = bounds.metadata;
  }
  for (const root of [roots.create, roots.update, roots.resource]) {
    for (const field of nodes[root].fields) {
      if (Object.hasOwn(bounds, field.name)) field.bounds = bounds[field.name];
    }
  }
}
if (mode === '--environments') addTemplateBounds(nodes, roots);
const result = {
  profile: mode === '--environments' ? 'q4t-openai-environment-types-v1' :
    mode === '--vault-credentials' ? 'q4t-openai-vault-credential-types-v1' :
    mode === '--agent-sessions' ? 'q4t-openai-agent-session-types-v1' :
    mode === '--agents' ? 'q4t-openai-agent-registration-types-v1' :
    mode === '--assistants' ? 'q4t-openai-assistant-types-v1' :
    mode === '--multi-agent' ? 'q4t-openai-multi-agent-types-v1' :
    mode === '--decisions' ? 'q4t-openai-decision-types-v1' :
    mode === '--live' ? 'q4t-openai-live-types-v1' : mode === '--realtime' ? 'q4t-openai-realtime-types-v1' :
    mode === '--api' ? 'q4t-openai-application-types-v1' :
      'q4t-openai-item-types-v1',
  sdk_version: '7.47.0',
  compiler_version: '5.9.3', roots, nodes,
  sources_sha256: Object.fromEntries([...sources].sort().map(path => [relative(sdk, path), hash(path)])),
  license_sha256: hash(resolve(sdk, 'LICENSE')),
  compiler_sha256: hash(resolve(compiler, 'lib/typescript.js')),
};
writeFileSync(resolve(output), JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify({ output: resolve(output), nodes: nodes.length,
  sources: sources.size, roots, sha256: hash(resolve(output)) }));
