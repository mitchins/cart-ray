import {privateRequest} from './protocol.mjs';
/** Operator workstation only: authenticated Wrangler remote bindings, no deployed gateway. */
import {getPlatformProxy} from 'wrangler';
let input='';for await(const part of process.stdin){input+=part;if(input.length>65536)throw new Error('operator request too large');}
const request=JSON.parse(input);
const forwarded=privateRequest(request);
const platform=await getPlatformProxy({configPath:process.argv[2],persist:false,remoteBindings:true});
try{
 const response=await platform.env[request.service].fetch(forwarded.url,forwarded.init);
 const text=await response.text();if(text.length>1048576)throw new Error('operator response too large');
 process.stdout.write(JSON.stringify({status:response.status,body:text}));
}finally{await platform.dispose();}
