import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import worker, {schedule,deliverHint} from '../automation-due-producer/worker.mjs';

function fixture() {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE review_automation_intents (store_key TEXT, environment TEXT, acquisition_id TEXT,
    outbox_id INTEGER, due_at INTEGER, PRIMARY KEY(store_key,environment,acquisition_id));`);
  db.exec(readFileSync(new URL('../migrations/0028_automation_due_schedule.sql',import.meta.url),'utf8'));
  const query = sql => ({bind(...params) {return {
    async run() {db.prepare(sql).run(...params);},
    async first() {return db.prepare(sql).get(...params) ?? null;},
  };}});
  const scheduled=[], due=[];
  const env={REVIEWS_ENVIRONMENT:'test',REVIEWS_DUE_SCHEDULER:'test',REVIEWS_STORE:'acorn',
    REVIEWS_DUE_SCHEDULER_TOKEN:'z'.repeat(32),REVIEWS_DB:{prepare:query},
    SCHEDULE_QUEUE:{async send(...x){scheduled.push(x);}}, DUE_QUEUE:{async send(...x){due.push(x);}}};
  return {db,env,scheduled,due};
}

test('accepted intent schedules durably and duplicate calls reuse one schedule',async()=>{
  const {db,env,scheduled,due}=fixture();
  db.prepare('INSERT INTO review_automation_intents VALUES (?,?,?,?,?)').run('acorn','test','purchase1',7,150);
  assert.equal(await schedule(7,env,100),'scheduled');
  assert.equal(await schedule(7,env,100),'scheduled');
  assert.equal(db.prepare('SELECT count(*) AS count FROM review_automation_due_schedule').get().count,1);
  assert.equal(scheduled[0][1].delaySeconds,50);
  assert.equal(await deliverHint(scheduled[0][0],env,150),'enqueued');
  assert.equal(await deliverHint(scheduled[1][0],env,150),'enqueued');
  assert.deepEqual(due[0],due[1]);
  assert.deepEqual(due[0][0],{outbox_id:7});
});

test('activation fence and store scope require persisted accepted intent',async()=>{
  const {db,env,scheduled}=fixture();
  db.prepare('INSERT INTO review_automation_intents VALUES (?,?,?,?,?)').run('other','test','purchase1',7,150);
  assert.equal(await schedule(7,env,100),'not_accepted');
  assert.equal(scheduled.length,0);
  assert.equal(await deliverHint({outbox_id:7},env,200),'discard');
});

test('long delays requeue boundedly and errors leave durable retry state',async()=>{
  const {db,env,scheduled,due}=fixture();
  db.prepare('INSERT INTO review_automation_intents VALUES (?,?,?,?,?)').run('acorn','test','purchase1',7,100000);
  env.SCHEDULE_QUEUE.send=async()=>{throw Error('queue unavailable');};
  await assert.rejects(schedule(7,env,100));
  assert.equal(db.prepare('SELECT count(*) AS count FROM review_automation_due_schedule').get().count,1);
  env.SCHEDULE_QUEUE.send=async(...args)=>scheduled.push(args);
  await schedule(7,env,100);
  assert.equal(scheduled[0][1].delaySeconds,43200);
  assert.equal(await deliverHint({outbox_id:7},env,43300),'deferred');
  assert.equal(due.length,0);
  await deliverHint({outbox_id:7},env,100000);
  assert.deepEqual(due[0][0],{outbox_id:7});
});

test('endpoint rejects unauthenticated and malformed requests and test-off configuration',async()=>{
  const {env}=fixture();
  const request=(token,body)=>new Request('https://internal/schedule',{method:'POST',headers:{authorization:`Bearer ${token}`,'content-type':'application/json'},body});
  assert.equal((await worker.fetch(request('wrong','{}'),env)).status,401);
  assert.equal((await worker.fetch(request(env.REVIEWS_DUE_SCHEDULER_TOKEN,'{"outbox_id":3,"email":"x"}'),env)).status,400);
  env.REVIEWS_DUE_SCHEDULER='disabled';
  assert.equal((await worker.fetch(request(env.REVIEWS_DUE_SCHEDULER_TOKEN,'{"outbox_id":3}'),env)).status,503);
});
test('stalled queue is bounded and retry reuses the persisted schedule',async()=>{
  const {db,env}=fixture();
  db.prepare('INSERT INTO review_automation_intents VALUES (?,?,?,?,?)').run('acorn','test','purchase1',7,150);
  env.SCHEDULE_QUEUE.send=()=>new Promise(()=>{});
  const start=Date.now(); await assert.rejects(schedule(7,env,100),/queue timeout/);
  assert.ok(Date.now()-start<3500);
  let calls=0; env.SCHEDULE_QUEUE.send=async()=>{calls++;};
  assert.equal(await schedule(7,env,100),'scheduled');
  assert.equal(calls,1);
  assert.equal(db.prepare('SELECT count(*) AS count FROM review_automation_due_schedule').get().count,1);
});
