export default {
  async fetch(request) {
    if (request.method === 'GET') return new Response('healthy');
    const reader = request.body.getReader();
    let timer;
    try {
      while (true) {
        const result = await Promise.race([
          reader.read(),
          new Promise(resolve => { timer = setTimeout(() => resolve('timeout'), 1000); }),
        ]);
        clearTimeout(timer);
        if (result === 'timeout') {
          await reader.cancel();
          console.log('handler returned 400 after timeout');
          return new Response('incomplete', { status: 400 });
        }
        if (result.done) {
          console.log('handler returned 400 for completed body');
          return new Response('invalid', { status: 400 });
        }
      }
    } finally {
      clearTimeout(timer);
      reader.releaseLock();
    }
  },
};
