// The SDK preserves these fields' types. The HTTP reference also constrains
// their lengths and counts. Keep the rules on the request fields, because
// multiple unrelated fields can share the same SDK string or array node.
// https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/environments/subresources/templates/methods/create
// https://developers.openai.com/api/reference/resources/beta/subresources/agents/subresources/environments/subresources/templates/methods/update
import {readFileSync} from 'node:fs';

export function addTemplateBounds(nodes, roots) {
  const {fields:rules} = JSON.parse(readFileSync(
    new URL('./environment_http_bounds.json', import.meta.url), 'utf8'));
  function fields(index, names) {
    const node = nodes[index];
    if (node.kind === 'union') return node.variants.flatMap(i => fields(i, names));
    if (node.kind === 'array') return fields(node.element, names);
    const field = node.fields?.find(entry => entry.name === names[0]);
    if (!field) return [];
    return names.length === 1 ? [field] : fields(field.type, names.slice(1));
  }
  for (const root of [roots.template_create, roots.template_update]) {
    for (const [path, bounds] of Object.entries(rules)) {
      const matches = fields(root, path.split('.'));
      if (!matches.length) throw new Error('Missing template HTTP field ' + path);
      for (const field of matches) field.bounds = bounds;
    }
  }
}
