export function routeArguments(method, path) {
  // Generated Node methods accept some path parameters positionally and others in params.
  const head = method.toString().split(')')[0];
  const names = head.slice(head.indexOf('(') + 1).split(',')
    .map(key => key.split('=')[0].trim()).filter(Boolean);
  const params = Object.fromEntries([...path.matchAll(/\{([^}]+)\}/g)].map(m => [m[1], 'q4tfixture']));
  return names.map(name => {
    if (/(?:ID|Id)$/.test(name) || Object.hasOwn(params, name) ||
        ['model', 'id', 'filename'].includes(name)) return 'q4tfixture';
    if (name === 'options') return {};
    return {...params, model: 'q4tfixture', input: 'fixture', encoding_format: 'float'};
  });
}
