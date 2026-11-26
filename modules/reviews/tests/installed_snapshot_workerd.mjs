/** Generated importer batches execute transactionally in real Workerd/D1, without a hosted mutation. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {Miniflare} from 'miniflare';

const input=JSON.parse(readFileSync(0,'utf8'));
const mf=new Miniflare({workers:[{config:{name:'installed-snapshot-test',type:'worker',
 compatibilityDate:'2026-08-31',
 manifest:{mainModule:'worker.mjs',modules:{'worker.mjs':{type:'esm',
 contents:'export default {fetch(){return new Response("test-only",{status:503})}}'}}},
 env:{REVIEWS_DB:{type:'d1',id:'installed-snapshot-test'}}}}]});
try {
 const db=await mf.getD1Database('REVIEWS_DB');
 const execute=sql=>db.batch(sql.map(statement=>db.prepare(statement)));
 const run=async operation=>{
  const result=await execute(operation.sql);
  assert.deepEqual(result.at(-1).results,[operation.expected],
   'the same transaction must return the exact installed operator readback');
  return result;
 };
 const rows=async sql=>(await db.prepare(sql).all()).results;
 const state=async()=>({
  publication:await rows('SELECT * FROM review_publications ORDER BY store_key,environment'),
  tables:await Promise.all(['review_snapshots','review_snapshot_provenance','review_snapshot_subjects',
   'review_contributions','review_import_titles','review_subject_aliases','review_import_guard'].map(async table=>
    [table,(await db.prepare(`SELECT COUNT(*) n FROM ${table}`).first()).n]))
 });
 for (const migration of input.migrations) await execute(migration);
 await run(input.original);
 const first=await state();
 await run(input.original);
 assert.deepEqual(await state(),first,'exact replay must not change publication or row counts');
 await assert.rejects(run(input.conflict));
 assert.deepEqual(await state(),first,'conflicting snapshot bytes must leave every table unchanged');
 await assert.rejects(run(input.wrong_previous));
 assert.deepEqual(await state(),first,'approval for an empty publication must not replace an existing one');
 await run(input.newer);
 const second=await state();
 assert.equal(second.publication[0].snapshot_id,'two');
 assert.equal(second.publication[0].revision,2);
 await assert.rejects(run(input.original));
 assert.deepEqual(await state(),second,'stale revision must not roll publication backwards');
 await assert.rejects(run(input.same_revision));
 assert.deepEqual(await state(),second,'another identity cannot reuse an existing publication revision');
 await db.prepare("CREATE TRIGGER installed_snapshot_silent_skip BEFORE INSERT ON review_contributions " +
  "WHEN NEW.snapshot_id='ignored-three' AND NEW.contribution_id='r' " +
  "BEGIN SELECT RAISE(IGNORE); END;").run();
 await assert.rejects(run(input.silent_skip));
 assert.deepEqual(await state(),second,'atomic count guard must reject a silently skipped contribution');
 for (const table of ['review_snapshots','review_snapshot_provenance','review_snapshot_subjects',
  'review_contributions','review_import_titles','review_subject_aliases']) {
  assert.equal((await db.prepare(`SELECT COUNT(*) n FROM ${table} WHERE snapshot_id=?`)
   .bind('ignored-three').first()).n,0,`${table}: silent skip left partial rows`);
 }
 await db.prepare('DROP TRIGGER installed_snapshot_silent_skip').run();
 await db.prepare("CREATE TRIGGER installed_snapshot_late_failure BEFORE UPDATE ON review_publications " +
  "BEGIN SELECT RAISE(ABORT, 'qualification: late publication failure'); END;").run();
 await assert.rejects(run(input.late_failure));
 assert.deepEqual(await state(),second,'late failure must roll back all preceding batch writes');
 for (const table of ['review_snapshots','review_snapshot_provenance','review_snapshot_subjects',
  'review_contributions','review_import_titles','review_subject_aliases']) {
  assert.equal((await db.prepare(`SELECT COUNT(*) n FROM ${table} WHERE snapshot_id=?`).bind('three').first()).n,0,
   `${table}: aborted snapshot left partial rows`);
 }
 assert.equal((await db.prepare('SELECT COUNT(*) n FROM review_import_guard').first()).n,0);
 await db.prepare('DROP TRIGGER installed_snapshot_late_failure').run();
 await run(input.late_failure);
 assert.equal((await db.prepare('SELECT snapshot_id FROM review_publications').first()).snapshot_id,'three');
 console.log(JSON.stringify({engine:'local Workerd/D1 binding batch',idempotent:true,conflict_rejected:true,
  stale_rejected:true,predecessor_cas_rejected:true,revision_collision_rejected:true,
  silent_skip_count_guard_rollback:true,late_failure_rollback:true,retry_after_failure:true,
  hosted_rest_atomicity:'not established by this local binding test'}));
} finally {await mf.dispose();}
