export async function jsonResponse(response, signal) {
  if (!response.ok || !response.body) throw new Error('evidence unavailable');
  const reader=response.body.getReader(); let size=0, text=''; const decoder=new TextDecoder();
  const cancel=()=>{void reader.cancel().catch(()=>{});};
  if (signal) {
    if (signal.aborted) cancel();
    else signal.addEventListener('abort',cancel,{once:true});
  }
  try {
    for (;;) {
      const x=await reader.read();
      if (x.done) break;
      size+=x.value.byteLength;
      if (size>65536) throw new Error('evidence too large');
      text+=decoder.decode(x.value,{stream:true});
    }
    return JSON.parse(text+decoder.decode());
  } finally {
    if (signal) signal.removeEventListener('abort',cancel);
    cancel();
  }
}
export async function bounded(fn) {
  const controller=new AbortController(); let timer;
  try { return await Promise.race([fn(controller.signal), new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('evidence timeout')),3000);
  })]); } finally {clearTimeout(timer);controller.abort();}
}
