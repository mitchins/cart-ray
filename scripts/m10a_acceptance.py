from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cartray.stripe import STRIPE_API_VERSION

_DEFAULT_API_BASE_URL = "https://cartray-test.mitch-336.workers.dev"
_DEFAULT_STATE_FILE = Path("/tmp/cartray-m10a-acceptance.json")
_FREE_PRODUCT_KEY = "TEST-FREE"
_MAX_RESPONSE_BYTES = 1_000_000
_TIMEOUT_SECONDS = 15
_STRIPE_KEY_RE = re.compile(r"(?:rk|sk)_(?:test|live)_[A-Za-z0-9_]+")
_SESSION_ID_RE = re.compile(r"^cs_[A-Za-z0-9_]+$")
_DESTINATION_ID_RE = re.compile(r"^(?:we|ed_test)_[A-Za-z0-9_]+$")


class AcceptanceError(RuntimeError):
    """An M10a acceptance operation could not prove its narrow test-only contract."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, _request, _fp, _code, _message, _headers, _new_url):
        return None


_OPENER = build_opener(_RejectRedirects())
RequestJson = Callable[[str, str, Mapping[str, str], bytes | None], Mapping[str, object]]


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the narrow, test-only M10a reconciliation acceptance harness.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    prepare = subcommands.add_parser(
        "prepare", help="disable the test destination and create two free CartRay Sessions"
    )
    prepare.add_argument("--destination-id", required=True, help="Stripe test Event Destination ID")
    prepare.add_argument("--api-base-url", default=_DEFAULT_API_BASE_URL, help="CartRay test Worker HTTPS base URL")
    prepare.add_argument(
        "--state-file", type=Path, default=_DEFAULT_STATE_FILE, help="local non-secret acceptance state file"
    )

    for name, help_text in (
        ("expire", "expire the prepared expiry Session through Stripe test mode"),
        ("d1-command", "print the targeted D1 command; never executes it"),
        ("verify-command", "print the targeted D1 evidence query; never executes it"),
        ("show", "show the prepared free Checkout URL and identifiers"),
    ):
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("--state-file", type=Path, default=_DEFAULT_STATE_FILE, help="local acceptance state file")
    enable = subcommands.add_parser("enable", help="re-enable the prepared Stripe test Event Destination")
    enable.add_argument("--state-file", type=Path, default=_DEFAULT_STATE_FILE, help="local acceptance state file")
    enable.add_argument(
        "--destination-id", help="recover a destination if prepare failed before its local state file was written"
    )
    return parser.parse_args(argv)


def _test_key(environ: Mapping[str, str]) -> str:
    key = environ.get("STRIPE_API_KEY")
    if not isinstance(key, str) or not key.startswith(("rk_test_", "sk_test_")):
        raise AcceptanceError("M10a acceptance requires an rk_test_ or sk_test_ STRIPE_API_KEY")
    return key


def _request_json(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Mapping[str, object]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_SECONDS) as response:
            return _decode_response(response.read(_MAX_RESPONSE_BYTES + 1))
    except HTTPError as error:
        payload = _decode_response(error.read(_MAX_RESPONSE_BYTES + 1))
        raise AcceptanceError(f"HTTP {error.code}: {payload.get('error', 'request failed')!r}") from error
    except URLError as error:
        raise AcceptanceError("network request failed") from error


def _decode_response(raw: bytes) -> Mapping[str, object]:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise AcceptanceError("response exceeded the configured size limit")
    try:
        decoded: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("response was not a JSON object") from error
    if not isinstance(decoded, dict):
        raise AcceptanceError("response was not a JSON object")
    return decoded


def _stripe_headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Stripe-Version": STRIPE_API_VERSION,
    }


def _event_destination(
    *, key: str, destination_id: str, action: str, request_json: RequestJson = _request_json
) -> Mapping[str, object]:
    if not _DESTINATION_ID_RE.fullmatch(destination_id):
        raise AcceptanceError("Stripe Event Destination ID is invalid")
    if action not in {"disable", "enable"}:
        raise ValueError("event destination action is invalid")
    payload = request_json(
        "POST",
        f"https://api.stripe.com/v2/core/event_destinations/{destination_id}/{action}",
        _stripe_headers(key),
        None,
    )
    if (
        payload.get("id") != destination_id
        or payload.get("livemode") is not False
        or payload.get("status") != f"{action}d"
    ):
        raise AcceptanceError(f"Stripe test Event Destination did not become {action}d")
    return payload


def _catalogue(api_base_url: str, request_json: RequestJson) -> tuple[str, str]:
    payload = request_json("GET", f"{api_base_url}/catalogue", {}, None)
    version, products = payload.get("version"), payload.get("products")
    if not isinstance(version, str) or not version.startswith("sha256:") or not isinstance(products, list):
        raise AcceptanceError("CartRay catalogue response is invalid")
    product = next(
        (item for item in products if isinstance(item, dict) and item.get("product_key") == _FREE_PRODUCT_KEY), None
    )
    if not isinstance(product, dict) or product.get("amount_minor") != 0 or product.get("max_quantity") != 1:
        raise AcceptanceError("CartRay test catalogue does not expose the expected free product")
    return version, _FREE_PRODUCT_KEY


def _create_checkout(
    *, api_base_url: str, manifest_version: str, product_key: str, case: str, request_json: RequestJson
) -> dict[str, str]:
    request_id = f"m10a-{case}-{secrets.token_hex(12)}"
    body = json.dumps(
        {
            "checkout_request_id": request_id,
            "manifest_version": manifest_version,
            "items": [{"product_key": product_key, "quantity": 1}],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    payload = request_json(
        "POST", f"{api_base_url}/checkout", {"Content-Type": "application/json"}, body
    )
    session_id, checkout_url = payload.get("session_id"), payload.get("checkout_url")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise AcceptanceError("CartRay did not return a valid Stripe Checkout Session ID")
    if not isinstance(checkout_url, str) or urlparse(checkout_url).scheme != "https":
        raise AcceptanceError("CartRay did not return an HTTPS Checkout URL")
    return {"request_id": request_id, "session_id": session_id, "checkout_url": checkout_url}


def _write_state(path: Path, state: Mapping[str, object]) -> None:
    if path.exists():
        raise AcceptanceError(f"state file already exists: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
        temporary.flush()
        os.fchmod(temporary.fileno(), 0o600)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _read_state(path: Path) -> dict[str, object]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceError("acceptance state file is unreadable") from error
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise AcceptanceError("acceptance state file has an unsupported schema")
    destination_id, sessions = raw.get("destination_id"), raw.get("sessions")
    if not isinstance(destination_id, str) or not _DESTINATION_ID_RE.fullmatch(destination_id):
        raise AcceptanceError("acceptance state file has an invalid destination")
    if not isinstance(sessions, dict):
        raise AcceptanceError("acceptance state file has no Sessions")
    for case in ("confirm", "expire"):
        session = sessions.get(case)
        if not isinstance(session, dict) or not isinstance(session.get("session_id"), str):
            raise AcceptanceError("acceptance state file has an invalid Session")
    return raw


def prepare(
    *, key: str, destination_id: str, api_base_url: str, state_file: Path, request_json: RequestJson = _request_json
) -> dict[str, object]:
    if state_file.exists():
        raise AcceptanceError(f"state file already exists: {state_file}")
    parsed = urlparse(api_base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise AcceptanceError("CartRay API base URL must be an absolute HTTPS URL without query or fragment")
    _event_destination(key=key, destination_id=destination_id, action="disable", request_json=request_json)
    version, product_key = _catalogue(api_base_url.rstrip("/"), request_json)
    state: dict[str, object] = {
        "schema": 1,
        "destination_id": destination_id,
        "api_base_url": api_base_url.rstrip("/"),
        "manifest_version": version,
        "product_key": product_key,
        "sessions": {
            "confirm": _create_checkout(
                api_base_url=api_base_url.rstrip("/"),
                manifest_version=version,
                product_key=product_key,
                case="confirm",
                request_json=request_json,
            ),
            "expire": _create_checkout(
                api_base_url=api_base_url.rstrip("/"),
                manifest_version=version,
                product_key=product_key,
                case="expire",
                request_json=request_json,
            ),
        },
    }
    _write_state(state_file, state)
    return state


def expire(*, key: str, state: Mapping[str, object], request_json: RequestJson = _request_json) -> Mapping[str, object]:
    session = state["sessions"]["expire"]
    assert isinstance(session, dict)
    session_id = session["session_id"]
    assert isinstance(session_id, str)
    payload = request_json(
        "POST", f"https://api.stripe.com/v1/checkout/sessions/{session_id}/expire", _stripe_headers(key), b""
    )
    if payload.get("id") != session_id or payload.get("livemode") is not False or payload.get("status") != "expired":
        raise AcceptanceError("Stripe did not expire the intended test Checkout Session")
    return payload


def _session_ids(state: Mapping[str, object]) -> tuple[str, str]:
    sessions = state["sessions"]
    assert isinstance(sessions, dict)
    confirm, expire = sessions["confirm"], sessions["expire"]
    assert isinstance(confirm, dict) and isinstance(expire, dict)
    confirm_id, expire_id = confirm["session_id"], expire["session_id"]
    assert isinstance(confirm_id, str) and isinstance(expire_id, str)
    return confirm_id, expire_id


def d1_command(state: Mapping[str, object]) -> str:
    confirm_id, expire_id = _session_ids(state)
    sql = f"""UPDATE checkout_sessions
SET updated_at = unixepoch() - 3700
WHERE external_session_id IN ('{confirm_id}', '{expire_id}')
  AND settlement_state = 'pending';

INSERT INTO checkout_reconciliations (order_id, session_id, next_attempt_at, updated_at)
SELECT order_id, external_session_id, 0, unixepoch()
FROM checkout_sessions
WHERE external_session_id IN ('{confirm_id}', '{expire_id}')
  AND settlement_state = 'pending'
ON CONFLICT(order_id) DO UPDATE SET
  next_attempt_at = 0,
  lease_token = NULL,
  lease_expires_at = NULL,
  updated_at = unixepoch();

SELECT external_session_id, settlement_state, updated_at
FROM checkout_sessions
WHERE external_session_id IN ('{confirm_id}', '{expire_id}')
ORDER BY external_session_id;"""
    return "uv run pywrangler d1 execute cartray-test --remote --config wrangler.toml --command " + shlex.quote(sql)


def verification_command(state: Mapping[str, object]) -> str:
    confirm_id, expire_id = _session_ids(state)
    sql = f"""SELECT
  cs.external_session_id,
  cs.order_id,
  cs.settlement_state,
  cs.stripe_payment_status,
  cs.stripe_amount_total_minor,
  r.attempt_count,
  r.last_outcome,
  r.last_error,
  r.observed_status,
  r.observed_payment_status,
  COALESCE(SUM(CASE WHEN o.event_type = 'OrderConfirmed' THEN 1 ELSE 0 END), 0) AS order_confirmed_count,
  (SELECT COUNT(*) FROM stripe_events AS se WHERE se.session_id = cs.external_session_id) AS stripe_event_count
FROM checkout_sessions AS cs
LEFT JOIN checkout_reconciliations AS r ON r.order_id = cs.order_id
LEFT JOIN outbox AS o ON o.order_id = cs.order_id
WHERE cs.external_session_id IN ('{confirm_id}', '{expire_id}')
GROUP BY cs.order_id
ORDER BY cs.external_session_id;"""
    return "uv run pywrangler d1 execute cartray-test --remote --config wrangler.toml --command " + shlex.quote(sql)


def _print_state(state: Mapping[str, object]) -> None:
    sessions = state["sessions"]
    assert isinstance(sessions, dict)
    confirm = sessions["confirm"]
    assert isinstance(confirm, dict)
    print(
        json.dumps(
            {
                "confirm_checkout_url": confirm["checkout_url"],
                "confirm_session_id": confirm["session_id"],
                "d1_command": d1_command(state),
                "expire_session_id": _session_ids(state)[1],
                "next": (
                    "Complete the free confirm Checkout, expire the other Session, then run d1-command "
                    "in the D1-token console."
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _safe_error(error: Exception) -> str:
    return _STRIPE_KEY_RE.sub("[redacted]", str(error))


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        if arguments.command == "prepare":
            state = prepare(
                key=_test_key(os.environ),
                destination_id=arguments.destination_id,
                api_base_url=arguments.api_base_url,
                state_file=arguments.state_file,
            )
            _print_state(state)
        elif arguments.command == "expire":
            state = _read_state(arguments.state_file)
            result = expire(key=_test_key(os.environ), state=state)
            print(json.dumps({"session_id": result["id"], "status": result["status"]}, separators=(",", ":")))
        elif arguments.command == "enable":
            if arguments.destination_id is not None:
                destination_id = arguments.destination_id
            else:
                state = _read_state(arguments.state_file)
                destination_id = state["destination_id"]
            result = _event_destination(
                key=_test_key(os.environ), destination_id=destination_id, action="enable"
            )
            print(json.dumps({"destination_id": result["id"], "status": result["status"]}, separators=(",", ":")))
        elif arguments.command == "d1-command":
            print(d1_command(_read_state(arguments.state_file)))
        elif arguments.command == "verify-command":
            print(verification_command(_read_state(arguments.state_file)))
        else:
            _print_state(_read_state(arguments.state_file))
    except (AcceptanceError, OSError, ValueError, KeyError) as error:
        print(f"M10a acceptance harness failed: {_safe_error(error)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
