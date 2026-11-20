// Read-only recovery polling spans the native worker's 60-second budget.
export async function recoverTrace(read, predicate, pause = (ms) =>
  new Promise((resolve) => setTimeout(resolve, ms))) {
  for (let i = 0; i < 70; ++i) {
    try {
      const page = await read(i);
      if (page.data.length === 1 && predicate(page.data[0])) return page.data[0];
    } catch (error) {
      if (error.status !== 503) throw error;
    }
    await pause(1000);
  }
  throw new Error('durable snapshot not observed within recovery bound');
}
