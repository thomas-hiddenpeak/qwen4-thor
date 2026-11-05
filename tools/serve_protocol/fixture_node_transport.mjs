// Only loaded by the explicit CPU-test import hook; never a production fallback.
export function nodeFetch() {
  return (input, init) => globalThis.fetch(input, init);
}
