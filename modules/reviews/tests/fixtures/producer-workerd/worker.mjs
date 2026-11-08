// Local-only qualification fixture: mocked control plane, real Workerd D1.
import {verifyInstallation,doctor,operatorSQL} from '../../../automation-intake-producer/operator.mjs';
import {relay,attest} from '../../../automation-intake-producer/worker.mjs';
import {deliver} from '../../../automation-intake-queue/worker.mjs';
const a='11111111-1111-4111-8111-111111111111',b='22222222-2222-4222-8222-222222222222',c='33333333-3333-4333-8333-333333333333',dbid='44444444-4444-4444-8444-444444444444';
const catalogue='sha256:'+'b'.repeat(64), mapping=JSON.stringify({[catalogue]:{kit:'subject'}});
const plain=(name,text)=>({name,type:'plain_text',text}), d1=name=>({name,type:'d1',database_id:dbid}), svc=(name,service)=>({name,type:'service',service});
export default {async fetch(request,bindings) {
  if (new URL(request.url).pathname!='/run') return new Response(null,{status:404});
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(mapping))),x=>x.toString(16).padStart(2,'0')).join('');
  const versions={source:{id:a,resources:{bindings:[d1('DB'),plain('CARTRAY_ENVIRONMENT','test'),plain('CARTRAY_REVIEWS_SOURCE','test'),plain('CARTRAY_REVIEWS_PRODUCER','test'),{name:'CF_VERSION_METADATA',type:'version_metadata'},svc('REVIEWS_SOURCE_TRIGGER','producer')]}}, producer:{id:b,resources:{bindings:[d1('COMMERCE_DB'),svc('SOURCE_API','source'),svc('CONSUMER_API','consumer'),svc('REVIEWS_API','reviews'),{name:'EVENT_QUEUE',type:'queue',queue_name:'queue1'}]}}, consumer:{id:'55555555-5555-4555-8555-555555555555',resources:{bindings:[{name:'CF_VERSION_METADATA',type:'version_metadata'},svc('REVIEWS_API','reviews'),plain('REVIEWS_ENVIRONMENT','test'),plain('REVIEWS_ORDER_SUBSCRIBER','test'),plain('SOURCE_STORE','acorn'),plain('SOURCE_ISSUER','issuer1'),plain('SOURCE_INSTALLATION','install1')]}}, reviews:{id:c,resources:{bindings:[d1('CARTRAY_DB'),{name:'CF_VERSION_METADATA',type:'version_metadata'},plain('REVIEWS_ENVIRONMENT','test'),plain('REVIEWS_MODE','verified_acquisition_only'),plain('REVIEWS_AUTOMATION_INTAKE','test'),plain('REVIEWS_STORE_KEY','acorn'),plain('REVIEWS_SUBJECT_MAPS',mapping)]}}};
  const sent=[];
  const e={REVIEWS_ENVIRONMENT:'test',REVIEWS_SOURCE_PRODUCER:'test',SOURCE_WORKER:'source',PRODUCER_WORKER:'producer',REVIEWS_WORKER:'reviews',CONSUMER_WORKER:'consumer',EVENT_QUEUE_NAME:'queue1',EVENT_QUEUE_ID:'e'.repeat(32),DEAD_LETTER_QUEUE_NAME:'dlq1',DEAD_LETTER_QUEUE_ID:'d'.repeat(32),SOURCE_STORE:'acorn',SOURCE_ISSUER:'issuer1',SOURCE_INSTALLATION:'install1',CF_ACCOUNT_ID:'a'.repeat(32),SOURCE_DATABASE_ID:dbid,STRIPE_ACCOUNT_ID:'acct_123',CATALOGUE_VERSION:catalogue,MAPPING_SHA256:digest,SOURCE_EVENT_KEY:'12'.repeat(32),CF_READ_TOKEN:'c'.repeat(32),SOURCE_IDENTITY_TOKEN:'s'.repeat(32),REVIEWS_ACTIVATION_TOKEN:'a'.repeat(32),TRIGGER_TOKEN:'t'.repeat(32),POLICY_VERSION:'policy1',DELAY_SECONDS:10,MAX_LATENESS:100,CF_VERSION_METADATA:{id:b},COMMERCE_DB:bindings.DB,EVENT_QUEUE:{async send(x){sent.push(x)}},SOURCE_API:{async fetch(){return Response.json({version_id:a,stripe_account:'acct_123',catalogue_version:catalogue})}},CONSUMER_API:{async fetch(){return Response.json({version_id:'55555555-5555-4555-8555-555555555555'})}},REVIEWS_API:{async fetch(url){return Response.json(url.includes('runtime-identity')?{version_id:c}:{status:'active',outbox_watermark:10})}}};
  const control=async url=>{const x=new URL(url).pathname.split('/');if(x.includes('queues')) return Response.json({success:true,result:x.at(-1)==='d'.repeat(32)?{queue_id:'d'.repeat(32),queue_name:'dlq1',consumers_total_count:0}:{queue_id:'e'.repeat(32),queue_name:'queue1',consumers_total_count:1,settings:{delivery_paused:false},consumers:[{type:'worker',script_name:'consumer',dead_letter_queue:'dlq1',settings:{max_retries:3}}]}});const i=x.indexOf('scripts'),v=versions[x[i+1]];return Response.json({success:true,result:x[i+2]==='deployments'?[{created_on:'2026-01-01',versions:[{version_id:v.id,percentage:100}]}]:v});};
  e.AUTHORITY_WORKER='authority';e.AUTHORITY_CONTEXT_DB_ID='77777777-7777-4777-8777-777777777777';
  const reject=async fn=>{try{await fn();throw Error('expected failure')}catch(error){if(error.message==='expected failure')throw error}};
  await reject(()=>relay(e,()=>{throw Error('control plane unavailable')}));
  const originalDB=e.COMMERCE_DB;
  const cliDB={withSession(){return this},prepare(sql){let values=[];return {bind(...args){values=args;return this},first(){return originalDB.prepare(operatorSQL(sql,values)).first()},all(){return originalDB.prepare(operatorSQL(sql,values)).all()}}}};
  const relayOnly={...e,COMMERCE_DB:cliDB,SOURCE_ISSUER:'relay-only',AUTHORITY_WORKER:undefined,AUTHORITY_CONTEXT_DB_ID:undefined};
  // Match the operator-verified consumer configuration for this second installation.
  const oldIssuer=versions.consumer.resources.bindings.find(x=>x.name==='SOURCE_ISSUER').text;
  versions.consumer.resources.bindings.find(x=>x.name==='SOURCE_ISSUER').text='relay-only';
  const four=await verifyInstallation(relayOnly,0,control);
  if(four.authority_version!==null || four.context_database_id!==null || four.authority_worker!==null)throw Error('four-worker record malformed');
  const stored=await originalDB.prepare("SELECT authority_version,context_database_id,authority_worker FROM reviews_verified_deployment WHERE subscriber_id='relay-only'").first();
  if(Object.values(stored).some(x=>x!==null))throw Error('CLI adapter did not preserve SQL NULL');
  await attest(relayOnly);
  versions.consumer.resources.bindings.find(x=>x.name==='SOURCE_ISSUER').text=oldIssuer;
  versions.authority={id:'66666666-6666-4666-8666-666666666666',resources:{bindings:[d1('COMMERCE_DB'),{name:'CONTEXT_DB',type:'d1',database_id:e.AUTHORITY_CONTEXT_DB_ID},svc('SOURCE_IDENTITY','source'),svc('CHECKOUT','source'),svc('ATTESTOR','producer'),svc('REVIEWS_API','reviews'),{name:'CF_VERSION_METADATA',type:'version_metadata'}]}};
  const record=await verifyInstallation(e,0,control);
  if(record.activation_status!=='verified' || record.revision!==1)throw Error('bootstrap verification failed');
  const health=await doctor(e,control);if(health.revision!==1 || health.status!=='verified')throw Error('doctor mismatch');
  await reject(()=>verifyInstallation(e,0,control));
  await reject(()=>bindings.DB.prepare('UPDATE reviews_verified_deployment SET revision=2').run());
  await reject(()=>bindings.DB.prepare('DELETE FROM reviews_verified_deployment').run());
  const unavailable=()=>{throw Error('control plane unavailable')};delete e.CF_READ_TOKEN;
  e.CF_VERSION_METADATA.id=a;await reject(()=>relay(e,unavailable));e.CF_VERSION_METADATA.id=b;
  for(const service of ['REVIEWS_API','CONSUMER_API']) {const original=e[service].fetch;e[service].fetch=async()=>Response.json({version_id:a});await reject(()=>relay(e,unavailable));e[service].fetch=original;}
  const first=await relay(e,unavailable),second=await relay(e,unavailable);
  // A reused issuer cannot certify a different immutable checkpoint scope.
  e.CF_READ_TOKEN='c'.repeat(32);
  const installed=await verifyInstallation(e,1,control);if(installed.activation_status!=='active')throw Error('matching checkpoint rejected');
  const installationBinding=versions.consumer.resources.bindings.find(x=>x.name==='SOURCE_INSTALLATION');
  installationBinding.text='replacement';
  await reject(()=>verifyInstallation({...e,SOURCE_INSTALLATION:'replacement'},0,control));
  const unhealthy=await doctor({...e,SOURCE_INSTALLATION:'replacement'},control);
  if(!unhealthy.mismatches.includes('checkpoint.installation_id'))throw Error('checkpoint collision not diagnosed');
  installationBinding.text='install1';
  const receiver={REVIEWS_ENVIRONMENT:'test',REVIEWS_ORDER_SUBSCRIBER:'test',CONSUMER_WORKER:'consumer',EVENT_QUEUE_NAME:'queue1',EVENT_QUEUE_ID:'e'.repeat(32),DEAD_LETTER_QUEUE_NAME:'dlq1',DEAD_LETTER_QUEUE_ID:'d'.repeat(32),SOURCE_STORE:'acorn',SOURCE_ISSUER:'issuer1',SOURCE_INSTALLATION:'install1',SOURCE_EVENT_KEY:e.SOURCE_EVENT_KEY,REVIEWS_AUTOMATION_TOKEN:'r'.repeat(32),REVIEWS_API:{fetch:async()=>Response.json({status:'accepted',due_at:100})}};
  const receipts=[]; for (const m of sent) receipts.push(await deliver(m,receiver));
  return Response.json({first,second,ids:sent.map(x=>x.outbox_id),receipts});
}};
