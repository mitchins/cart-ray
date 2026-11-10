/** Operator-only bridge to PR A verifier using authenticated existing services. */
import {getPlatformProxy} from 'wrangler';
import {inspectInstallation,installationFields,verifyInstallation,doctor} from '../automation-intake-producer/operator.mjs';
let text='';for await(const part of process.stdin){text+=part;if(text.length>65536)throw new Error('operator input too large');}
const input=JSON.parse(text), env=input.env;
const platform=await getPlatformProxy({configPath:process.argv[2],persist:false,remoteBindings:true});
const root=`https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/d1/database/${env.SOURCE_DATABASE_ID}/query`;
const execute=async(sql,params=[])=>{
 const response=await fetch(root,{method:'POST',headers:{authorization:`Bearer ${env.CF_READ_TOKEN}`,'content-type':'application/json'},body:JSON.stringify({sql,params}),signal:AbortSignal.timeout(30000)});
 if(!response.ok)throw new Error(`operator D1 read/write HTTP ${response.status}`);
 const value=await response.json();if(value.success!==true||value.result?.[0]?.success!==true)throw new Error('operator D1 operation failed');
 return value.result[0];
};
env.COMMERCE_DB={withSession(){return this},prepare(sql){let args=[];return {bind(...values){args=values;return this},async first(){return (await execute(sql,args)).results?.[0]??null},all(){return execute(sql,args)}}}};
env.SOURCE_API=platform.env.SOURCE_API;env.REVIEWS_API=platform.env.REVIEWS_API;env.EVENT_QUEUE={};
try{
 let result;
 if(input.mode==='inspect')result=installationFields(env,await inspectInstallation(env));
 else if(input.mode==='record')result=await verifyInstallation(env,input.expectedRevision);
 else if(input.mode==='doctor')result=await doctor(env);
 else throw new Error('unknown verifier operation');
 process.stdout.write(JSON.stringify(result));
}finally{await platform.dispose();}
