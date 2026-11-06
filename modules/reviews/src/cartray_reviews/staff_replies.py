"""Test-only staff replies with immutable revisions and atomic publication invalidation."""

import hashlib
import json
import re

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _rows(result):
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


async def mutate_reply(db, *, store, environment, review_id, data, action, key, actor, now, random_token):
    """Publish, edit or withdraw one native reply using optimistic, idempotent writes."""
    if (
        environment != "test"
        or action not in ("upsert", "withdraw")
        or not _HEX.fullmatch(review_id)
        or not _KEY.fullmatch(key)
    ):
        raise ValueError("invalid reply")
    required = {"parent_revision_id", "expected_reply_revision_id"}
    if action == "upsert":
        required |= {"author", "body"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("invalid reply")
    parent_revision, expected = data["parent_revision_id"], data["expected_reply_revision_id"]
    if not isinstance(parent_revision, str) or not _HEX.fullmatch(parent_revision):
        raise ValueError("invalid reply")
    if expected is not None and (not isinstance(expected, str) or not _HEX.fullmatch(expected)):
        raise ValueError("invalid reply")
    if action == "withdraw" and expected is None:
        raise ValueError("invalid reply")
    if action == "upsert" and (
        not isinstance(data["author"], str)
        or not data["author"].strip()
        or len(data["author"]) > 120
        or not isinstance(data["body"], str)
        or len(data["body"].strip()) < 3
        or len(data["body"]) > 5000
    ):
        raise ValueError("invalid reply")
    payload_hash = hashlib.sha256(
        json.dumps([review_id, action, data], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()

    async def existing_request():
        return _rows(
            await db.prepare(
                "SELECT actor,payload_hash,reply_id,reply_revision_id,state FROM native_staff_reply_requests "
                "WHERE store_key=? AND environment=? AND request_key=?"
            )
            .bind(store, environment, key)
            .all()
        )

    previous = await existing_request()
    if previous:
        record = previous[0]
        if record["actor"] != actor or record["payload_hash"] != payload_hash:
            raise ValueError("reply conflict")
        return {
            "reply_id": record["reply_id"],
            "reply_revision_id": record["reply_revision_id"],
            "state": record["state"],
        }
    parent = _rows(
        await db.prepare(
            "SELECT subject_id FROM native_reviews WHERE store_key=? AND environment=? AND review_id=? "
            "AND revision_id=? AND state='published'"
        )
        .bind(store, environment, review_id, parent_revision)
        .all()
    )
    if len(parent) != 1:
        raise ValueError("reply conflict")
    heads = _rows(
        await db.prepare(
            "SELECT reply_id,reply_revision_id,state FROM native_staff_replies "
            "WHERE store_key=? AND environment=? AND review_id=?"
        )
        .bind(store, environment, review_id)
        .all()
    )
    if expected is None:
        if action != "upsert" or heads:
            raise ValueError("reply conflict")
        reply_id, operation = random_token(), "created"
    else:
        if len(heads) != 1 or heads[0]["state"] != "published" or heads[0]["reply_revision_id"] != expected:
            raise ValueError("reply conflict")
        reply_id, operation = heads[0]["reply_id"], "withdrawn" if action == "withdraw" else "edited"
    revision = expected if action == "withdraw" else random_token()
    state = "withdrawn" if action == "withdraw" else "published"
    event_id, subject = random_token(), parent[0]["subject_id"]
    if expected is None:
        guard_reply = (
            "NOT EXISTS(SELECT 1 FROM native_staff_replies h WHERE h.store_key=? AND h.environment=? AND h.review_id=?)"
        )
        guard_args = (store, environment, review_id)
    else:
        guard_reply = (
            "EXISTS(SELECT 1 FROM native_staff_replies h WHERE h.store_key=? AND h.environment=? "
            "AND h.review_id=? AND h.reply_id=? AND h.reply_revision_id=? AND h.state='published')"
        )
        guard_args = (store, environment, review_id, reply_id, expected)
    statements = [
        db.prepare(
            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM native_reviews r "
            "JOIN native_publication_versions p "
            "ON p.store_key=r.store_key AND p.environment=r.environment AND p.subject_id=r.subject_id "
            "WHERE r.store_key=? AND r.environment=? AND r.review_id=? AND r.revision_id=? "
            "AND r.state='published') AND " + guard_reply + " THEN 1 ELSE 0 END"
        ).bind(store, environment, review_id, parent_revision, *guard_args)
    ]
    if action == "upsert":
        statements.append(
            db.prepare("INSERT INTO native_staff_reply_revisions VALUES (?,?,?,?,?,?,?)").bind(
                store, environment, reply_id, revision, data["author"], data["body"], now
            )
        )
    if expected is None:
        statements.append(
            db.prepare("INSERT INTO native_staff_replies VALUES (?,?,?,?,?,?,'published')").bind(
                store, environment, review_id, reply_id, revision, parent_revision
            )
        )
    else:
        statements.append(
            db.prepare(
                "UPDATE native_staff_replies SET reply_revision_id=?,parent_revision_id=?,state=? WHERE store_key=? "
                "AND environment=? AND review_id=? AND reply_revision_id=? AND state='published'"
            ).bind(revision, parent_revision, state, store, environment, review_id, expected)
        )
    statements.extend(
        [
            db.prepare("INSERT INTO native_staff_reply_requests VALUES (?,?,?,?,?,?,?,?)").bind(
                store, environment, key, actor, payload_hash, reply_id, revision, state
            ),
            db.prepare("INSERT INTO native_staff_reply_audit VALUES (?,?,?,?,?,?,?,?,?)").bind(
                event_id, store, environment, review_id, reply_id, revision, actor, operation, now
            ),
            db.prepare(
                "UPDATE native_publication_versions SET version=version+1 "
                "WHERE store_key=? AND environment=? AND subject_id=?"
            ).bind(store, environment, subject),
            db.prepare("INSERT INTO native_effect_outbox VALUES (?,?,?,?,?,?)").bind(
                event_id, store, environment, subject, "invalidate", now
            ),
            db.prepare("DELETE FROM native_guard"),
        ]
    )
    try:
        await db.batch(statements)
    except Exception as exc:
        previous = await existing_request()
        if previous and previous[0]["actor"] == actor and previous[0]["payload_hash"] == payload_hash:
            record = previous[0]
            return {
                "reply_id": record["reply_id"],
                "reply_revision_id": record["reply_revision_id"],
                "state": record["state"],
            }
        raise ValueError("reply conflict") from exc
    return {"reply_id": reply_id, "reply_revision_id": revision, "state": state}


async def public_replies(db, store, environment, review_ids, *, unified=False):
    """Fetch at most one published reply per selected native root in one indexed read."""
    if not review_ids:
        return {}
    if len(review_ids) > 50:
        raise ValueError("too many review identities")
    placeholders = ",".join("?" for _ in review_ids)
    result = _rows(
        await db.prepare(
            "SELECT h.review_id,h.reply_id,v.author,v.body,v.created_at FROM native_staff_replies h "
            "JOIN native_reviews r ON r.store_key=h.store_key AND r.environment=h.environment "
            "AND r.review_id=h.review_id "
            "AND r.revision_id=h.parent_revision_id AND r.state='published' "
            "JOIN native_staff_reply_revisions v ON v.store_key=h.store_key AND v.environment=h.environment "
            "AND v.reply_id=h.reply_id AND v.reply_revision_id=h.reply_revision_id "
            "WHERE h.store_key=? AND h.environment=? AND h.state='published' AND h.review_id IN (" + placeholders + ")"
        )
        .bind(store, environment, *review_ids)
        .all()
    )
    output = {}
    for row in result:
        reply = {
            "id": ("n-" if unified else "") + row["reply_id"],
            "kind": "reply",
            "source": {"kind": "native", "attribution": None},
            "author": row["author"],
            "body": row["body"],
            "rating": None,
            "created_at": row["created_at"],
            "verification": "unverified",
            "replies": [],
        }
        if unified:
            reply["source_id"] = row["reply_id"]
        output[row["review_id"]] = reply
    return output
