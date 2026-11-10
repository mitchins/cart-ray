/** Preserve the existing private HTTP contract through Wrangler's remote proxy. */
export function privateRequest(input){
 if(!['REVIEWS','PRODUCER','CONSUMER','EVIDENCE'].includes(input.service)||
    !['GET','POST'].includes(input.method||'GET')||typeof input.path!=='string'||
    !input.path.startsWith('/')||input.path.startsWith('//'))throw new Error('invalid private service request');
 const body=input.body===undefined?undefined:JSON.stringify(input.body);
 const headers=new Headers(input.headers||{});
 if(body!==undefined){headers.set('content-type','application/json');headers.set('content-length',String(new TextEncoder().encode(body).length));}
 return {url:'https://operator.internal'+input.path,init:{method:input.method||'GET',headers,redirect:'manual',
  ...(body===undefined?{}:{body}),signal:AbortSignal.timeout(30000)}};
}
