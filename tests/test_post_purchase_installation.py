"""No-backfill initialisation must be atomic, test-only, and non-repairing."""

import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pp_installation", ROOT / "scripts/post_purchase_installation.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE outbox(id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE orders(order_id TEXT PRIMARY KEY)")
    for name in ("0006_post_purchase_actions.sql", "0007_post_purchase_checkpoint.sql"):
        db.executescript((ROOT / "migrations" / name).read_text())
    return db


def batch(db, apply=False, queries=None):
    with db:
        for item in queries or installer.statements("merchant", apply):
            rows = db.execute(item["sql"], item["params"]).fetchall()
    return dict(rows[0])


def test_default_dry_run_and_atomic_fence_no_backfill():
    with database() as db:
        db.executemany("INSERT INTO outbox VALUES (?)", [(1,), (7,)])
        status = batch(db)
        assert installer.classify(status, "merchant") == "uninitialised"
        assert status["installation_count"] == status["checkpoint_count"] == 0
        status = batch(db, True)
        assert installer.classify(status, "merchant") == "retained"
        assert status["activation_watermark"] == status["last_outbox_id"] == 7
        db.execute("INSERT INTO outbox VALUES (8)")
        db.execute("UPDATE post_purchase_checkpoint SET last_outbox_id=8")
        again = batch(db, True)
        assert again["activation_watermark"] == 7
        assert again["last_outbox_id"] == 8  # Never resets an advanced cursor.


def test_empty_outbox_initialises_zero_and_partial_insert_rolls_back():
    with database() as db:
        queries = installer.statements("merchant", True)
        queries[2] = {"sql": "INSERT INTO missing_table VALUES (1)", "params": []}
        with pytest.raises(sqlite3.OperationalError):
            batch(db, queries=queries)
        assert db.execute("SELECT COUNT(*) FROM post_purchase_installation").fetchone()[0] == 0
        assert batch(db, True)["activation_watermark"] == 0


@pytest.mark.parametrize("seed", [
    "INSERT INTO post_purchase_installation VALUES (1,'merchant','test')",
    "INSERT INTO post_purchase_checkpoint VALUES (1,'merchant','test',0,0)",
    "INSERT INTO post_purchase_installation VALUES (1,'other','test');"
    "INSERT INTO post_purchase_checkpoint VALUES (1,'other','test',0,0)",
    "INSERT INTO post_purchase_installation VALUES (1,'merchant','live');"
    "INSERT INTO post_purchase_checkpoint VALUES (1,'merchant','live',0,0)",
    "INSERT INTO post_purchase_installation VALUES (1,'merchant','test');"
    "INSERT INTO post_purchase_checkpoint VALUES (1,'merchant','test',5,5)",
])
def test_partial_or_changed_scope_refused_without_repair(seed):
    with database() as db:
        db.executescript(seed)
        before = list(db.iterdump())
        with pytest.raises(ValueError, match="no repair"):
            installer.classify(batch(db), "merchant")
        with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
            batch(db, True)
        assert list(db.iterdump()) == before


def test_scope_guard_rechecks_after_dry_run():
    with database() as db:
        assert installer.classify(batch(db), "merchant") == "uninitialised"
        db.executescript("INSERT INTO post_purchase_installation VALUES (1,'other','test')")
        with pytest.raises(sqlite3.OperationalError):
            batch(db, True)
        assert db.execute("SELECT COUNT(*) FROM post_purchase_checkpoint").fetchone()[0] == 0


@pytest.mark.parametrize("merchant,environment", [("other", "test"), ("merchant", "live"), ("other", "live")])
def test_retained_foreign_executions_are_rejected_by_status_and_atomic_guard(merchant, environment):
    with database() as db:
        batch(db, True)
        db.execute("INSERT INTO orders VALUES ('order-one')")
        db.execute("INSERT INTO post_purchase_executions VALUES (?,?,?,?,?,'claimed',NULL,0,0)",
                   (merchant, environment, "order-one", "workflow-one", "a" * 64))
        db.commit()
        before = list(db.iterdump())
        status = batch(db)
        assert status["foreign_execution_count"] == 1
        with pytest.raises(ValueError, match="no repair"):
            installer.classify(status, "merchant")
        with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
            batch(db, True)
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("state", ["claimed", "accepted", "uncertain"])
def test_retained_valid_execution_claims_are_unchanged(state):
    with database() as db:
        batch(db, True)
        db.execute("INSERT INTO orders VALUES ('order-one')")
        db.execute("INSERT INTO post_purchase_executions VALUES ('merchant','test',?,?,?,?,?,0,0)",
                   ("order-one", "workflow-one", "a" * 64, state, "provider-reference"))
        db.commit()
        before = list(db.iterdump())
        assert installer.classify(batch(db, True), "merchant") == "retained"
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("mode,environment,database_id", [
    ("test", "test", "db"), ("disabled", "live", "db"), ("disabled", "test", "other"),
])
def test_backend_must_be_correct_test_binding_with_processing_disabled(mode, environment, database_id):
    installation = SimpleNamespace(db=lambda role: "db", name=lambda role: "merchant-test-" + role,
                                   state={"deployment": {"versions": {"source": "v1"}}})
    api = SimpleNamespace(request=lambda path: {"name": "merchant-test-commerce"}, version=lambda name: {
        "id": "v1", "resources": {"bindings": [
            {"name": "DB", "type": "d1", "database_id": database_id},
            {"name": "CARTRAY_ENVIRONMENT", "type": "plain_text", "text": environment},
            {"name": "CARTRAY_POST_PURCHASE_MODE", "type": "plain_text", "text": mode},
        ]}})
    with pytest.raises(ValueError, match="processing disabled"):
        installer.validate_backend(installation, api)


def test_real_d1_batch_rolls_back_both_rows_on_failure():
    # Local workerd D1, not a synthetic REST transaction assumption or remote write.
    script = """
import {Miniflare} from 'miniflare';
let raw='';for await(const chunk of process.stdin)raw+=chunk;
const input=JSON.parse(raw);
const worker=`export default {async fetch(request,env){
 const input=await request.json(),db=env.DB;
 for(const sql of input.schema)await db.prepare(sql).run();
 const prepared=items=>items.map(item=>item.params.length
  ?db.prepare(item.sql).bind(...item.params):db.prepare(item.sql));
 const broken=structuredClone(input.queries);broken[2]={sql:'INSERT INTO missing_table VALUES (1)',params:[]};
 let failed=false;try{await db.batch(prepared(broken));}catch{failed=true;}
 if(!failed)throw Error('expected batch failure');
 const count=await db.prepare('SELECT COUNT(*) AS n FROM post_purchase_installation').first();
 if(count.n!==0)throw Error('partial commit');
 const result=await db.batch(prepared(input.queries));
 if(result.at(-1).results[0].activation_watermark!==7)throw Error('wrong fence');
 const repeat=await db.batch(prepared(input.queries));
 if(repeat.at(-1).results[0].last_outbox_id!==7)throw Error('changed fence');
 await db.prepare("INSERT INTO post_purchase_executions VALUES ('other','live')").run();
 let rejected=false;try{await db.batch(prepared(input.queries));}catch{rejected=true;}
 if(!rejected)throw Error('foreign execution accepted');
 const status=await db.prepare(input.queries.at(-1).sql).bind('merchant').first();
 if(status.foreign_execution_count!==1)throw Error('foreign execution not reported');
 return Response.json({ok:true});
}}`;
const mf=new Miniflare({workers:[{config:{name:'pp-initialisation-test',type:'worker',
 compatibilityDate:'2026-08-31',env:{DB:{type:'d1',name:'test-db',id:'test-db'}},
 manifest:{mainModule:'worker.mjs',modules:{'worker.mjs':{type:'esm',
 contents:worker}}}}}]});
try {
 const response=await mf.dispatchFetch('https://local.invalid/',{method:'POST',body:JSON.stringify(input)});
 if(response.status!==200)throw Error(await response.text());
}finally{await mf.dispose();}
"""
    schema = ["CREATE TABLE outbox(id INTEGER PRIMARY KEY)", "INSERT INTO outbox VALUES (7)",
              "CREATE TABLE orders(order_id TEXT PRIMARY KEY)",
              "CREATE TABLE post_purchase_installation(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
              "merchant_id TEXT NOT NULL,environment TEXT NOT NULL)",
              "CREATE TABLE post_purchase_checkpoint(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
              "merchant_id TEXT NOT NULL,environment TEXT NOT NULL,activation_watermark INTEGER NOT NULL,"
              "last_outbox_id INTEGER NOT NULL)",
              "CREATE TABLE post_purchase_executions(merchant_id TEXT,environment TEXT)"]
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                            input=json.dumps({"schema": schema, "queries": installer.statements("merchant", True)}),
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
