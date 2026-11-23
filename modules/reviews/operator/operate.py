"""Resumable, test-only CartRay installation operator. Run with --help."""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from cloud import API, CloudError, stripe
from merchant_inputs import ConnectedShell, commerce_build_hash, javascript_json, tree_hash
from merchant_inputs import snapshot as snapshot_merchant_inputs
from merchant_inputs import validate as validate_merchant_inputs
from post_purchase_operator import (
    OFF,
    ON,
    POST_PURCHASE_ROLES,
    PROVIDER_KEYS,
    PostPurchaseOperator,
    eligible_offers,
    validate_email_credential,
)
from topology import graph, post_purchase_secret_bindings, secret_bindings, validate_post_purchase, wrangler_config

from cartray.merchant_contract import merchant_path, register_path
from cartray.order_access import validate_resource_configuration

ROOT = Path(__file__).resolve().parents[3]
DATABASES = ("commerce",)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_private(path, *, raw=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.geteuid() or mode.st_mode & 0o077:
            raise ValueError("credential/vault file must be a private regular file owned by the operator (chmod 600)")
        with os.fdopen(fd, "rb" if raw else "r", closefd=False) as stream:
            return stream.read(32769) if raw else json.load(stream)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        os.chmod(stream.name, 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)


def validate_config(config):
    required = {"namespace", "account_id", "stripe_account_id", "preflight_lock"}
    optional = {
        "merchant_name",
        "merchant_contact",
        "delay_seconds",
        "max_lateness",
        "reviews_enabled",
        "review_requests",
        "theme",
        "gel",
        "theme_content",
        "gel_assets",
        "merchant_inputs",
        "merchant_inputs_sha256",
        "store_key",
        "subject_map",
        "subject_details",
        "review_invitation_default_checked",
        "storefront_origin",
        "success_path",
        "cancel_path",
        "webhook_url",
        "source_mode",
        "source_api_path",
        "post_purchase_config",
        "post_purchase_config_sha256",
        "post_purchase_email_provider",
        "fulfilment",
    }
    if not isinstance(config, dict) or set(config) - required - optional or required - set(config):
        raise ValueError("installation config has missing or unknown fields")
    for field in ("storefront_origin", "webhook_url"):
        if field not in config:
            continue
        value = config[field]
        try:
            parsed = urlsplit(value) if isinstance(value, str) else None
            valid = (parsed is not None and parsed.scheme == "https" and parsed.hostname
                     and re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", parsed.hostname)
                     and not parsed.username and not parsed.password and not parsed.port
                     and not parsed.query and not parsed.fragment
                     and value == "https://" + parsed.hostname + parsed.path)
            valid = valid and (not parsed.path if field == "storefront_origin" else
                               re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", parsed.path))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                f"invalid {field}: expected an HTTPS origin or explicit webhook path without credentials/query"
            )
    for field in ("success_path", "cancel_path", "source_api_path"):
        if field in config and (not isinstance(config[field], str)
                                or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?)?", config[field])):
            raise ValueError(f"invalid {field}: expected a local path without query or traversal")
    if config.get("source_mode", "public") not in ("public", "private"):
        raise ValueError("source_mode must be public or private")
    if config.get("source_mode") == "private":
        if not all(field in config for field in ("storefront_origin", "source_api_path", "webhook_url")):
            raise ValueError("private source requires storefront_origin, source_api_path and webhook_url")
        if config["source_api_path"] == "/" or config["source_api_path"].endswith("/"):
            raise ValueError("source_api_path must be a non-root prefix without trailing slash")
        if not config["webhook_url"].startswith(config["storefront_origin"] + "/"):
            raise ValueError("private source webhook must use the external storefront origin")
    elif "source_api_path" in config:
        raise ValueError("source_api_path requires private source mode")
    if "storefront_origin" in config and "merchant_inputs" not in config:
        raise ValueError("external storefront requires an explicit merchant_inputs package")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,31}", config["namespace"]) or not config["namespace"].endswith("-test"):
        raise ValueError("namespace must be 3–32 lower-case characters ending in -test")
    if not re.fullmatch(r"[a-f0-9]{32}", config["account_id"]):
        raise ValueError("invalid Cloudflare account identity")
    if not re.fullmatch(r"acct_[A-Za-z0-9]+", config["stripe_account_id"]):
        raise ValueError("invalid Stripe test account identity")
    for field, default, maximum in [("delay_seconds", 0, 2592000), ("max_lateness", 86400, 2592000)]:
        value = config.get(field, default)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"invalid {field}")
    for name, limit in [("merchant_name", 100), ("merchant_contact", 200)]:
        if name in config and (
            not isinstance(config[name], str)
            or not config[name].strip()
            or len(config[name]) > limit
            or re.search(r"[\x00-\x1f\x7f]", config[name])
        ):
            raise ValueError(f"invalid public {name}")
    if 'review_requests' in config:
        from cartray.order_reviews import ReviewSettings
        ReviewSettings.parse(json.dumps(config['review_requests']))
    if "fulfilment" in config:
        fulfilment = config["fulfilment"]
        if ("merchant_inputs" not in config or not isinstance(fulfilment, dict)
                or set(fulfilment) != {"buckets"} or not isinstance(fulfilment["buckets"], dict)
                or any(not isinstance(k, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", k)
                       or not isinstance(v, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", v)
                       for k, v in fulfilment["buckets"].items())):
            raise ValueError("fulfilment requires merchant inputs and valid private bucket bindings")
    if type(config.get("reviews_enabled", True)) is not bool:
        raise ValueError("reviews_enabled must be a boolean")
    if type(config.get("review_invitation_default_checked", True)) is not bool:
        raise ValueError("review_invitation_default_checked must be a boolean")
    if config.get("theme", "minimal") not in ("minimal", "studio", "classic"):
        raise ValueError("theme must be minimal, studio or classic")
    for field in ("gel", "theme_content", "gel_assets"):
        if field in config and (not isinstance(config[field], str) or not config[field].strip()):
            raise ValueError(f"invalid {field} path")
    if any(field in config for field in ("theme_content", "gel_assets")) and "gel" not in config:
        raise ValueError("custom theme_content or gel_assets requires a GEL manifest")
    if "merchant_inputs" in config:
        if any(field in config for field in ("gel", "theme_content", "gel_assets")):
            raise ValueError("merchant inputs already include GEL and storefront content; rebuild the package instead")
        if (not isinstance(config["merchant_inputs"], str) or not config["merchant_inputs"].strip()
                or not isinstance(config.get("merchant_name"), str) or not config["merchant_name"].strip()
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", config.get("store_key", ""))
                or not isinstance(config.get("subject_map"), dict)
                or not isinstance(config.get("subject_details"), list)):
            raise ValueError("merchant inputs require a store key and explicit review subject mappings")
        mapping, details = config["subject_map"], config["subject_details"]
        if (len(mapping) > 1000
                or any(not isinstance(k, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,63}", k) or
                       not isinstance(v, str) or not re.fullmatch(r"[a-z0-9-]{1,128}", v)
                       for k, v in mapping.items())
                or any(not isinstance(x, list) or len(x) != 3 or not all(isinstance(v, str) for v in x)
                       or not re.fullmatch(r"[a-z0-9-]{1,128}", x[0])
                       or not merchant_path(x[1], product=True)
                       or not x[2].strip() or len(x[2]) > 200 for x in details)
                or len({x[0] for x in details}) != len(details)
                or len({x[1].casefold() for x in details}) != len(details)
                or not set(mapping.values()).issubset({x[0] for x in details})):
            raise ValueError("invalid merchant review subject mappings")
        prefixes = {}
        for _subject, route, _label in details:
            register_path(route, prefixes)
        if "merchant_inputs_sha256" in config and not re.fullmatch(r"[a-f0-9]{64}", config["merchant_inputs_sha256"]):
            raise ValueError("invalid merchant inputs fingerprint")
    elif any(k in config for k in ("merchant_inputs_sha256", "store_key", "subject_map", "subject_details")):
        raise ValueError("merchant identity requires merchant inputs")
    elif config.get("review_invitation_default_checked", True) is not True:
        raise ValueError("custom invitation checkbox default requires a merchant inputs package")
    if "post_purchase_config" in config:
        if config.get("post_purchase_email_provider", "smtp2go") not in PROVIDER_KEYS:
            raise ValueError("unsupported post-purchase email provider")
        snapshot = config["post_purchase_config"]
        if (not isinstance(snapshot, str) or not snapshot.strip()
                or not config.get("store_key")):
            raise ValueError(
                "test order confirmations require a merchant identity"
            )
        if "post_purchase_config_sha256" in config and (
                not isinstance(config["post_purchase_config_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", config["post_purchase_config_sha256"])):
            raise ValueError("invalid post-purchase configuration fingerprint")
    elif "post_purchase_config_sha256" in config or "post_purchase_email_provider" in config:
        raise ValueError("post-purchase fingerprint requires a protected action file")
    return {
        "reviews_enabled": True,
        "review_invitation_default_checked": True,
        "merchant_name": "Squirrel’s Acorn Store",
        "merchant_contact": "Contact the test merchant",
        "delay_seconds": 0,
        "max_lateness": 86400,
        "theme": "minimal",
        **config,
    }


class Installation(PostPurchaseOperator):
    def __init__(self, directory, credentials=None, api=None, *, credential_provider=None):
        self.directory = Path(directory).resolve()
        self.state_path = self.directory / "inventory.json"
        self.vault_path = self.directory / "secrets.json"
        self.state = json.loads(self.state_path.read_text())
        self.config = validate_config(self.state["config"])
        if (any(k.startswith('queue:') for k in self.state.get('resources', {}))
                or self.state.get('post_purchase_provider_migration')):
            raise ValueError('retained legacy delivery installation: use its pinned release; '
                             'no automatic migration, secret replacement or resend')
        if (
            self.state.get("format") != 2
            or self.state.get("environment") != "test"
            or self.state.get("installation") != self.config["namespace"]
        ):
            raise ValueError("inventory scope/format mismatch; no resource operation is permitted")
        if self.has_post_purchase:
            if (self.config["post_purchase_config"] != "inputs/post-purchase.json"
                    or "post_purchase_config_sha256" not in self.config):
                raise ValueError("post-purchase inventory requires a fingerprinted installation snapshot")
            self.config["post_purchase_config"] = str(self.directory / self.config["post_purchase_config"])
        if not Path(self.config["preflight_lock"]).is_absolute():
            self.config["preflight_lock"] = str(self.directory / self.config["preflight_lock"])
        for field in ("gel", "theme_content", "gel_assets", "merchant_inputs"):
            if field in self.config and not Path(self.config[field]).is_absolute():
                self.config[field] = str(self.directory / self.config[field])
        self.credentials = credentials
        if credentials is not None:
            required = {
                "cloudflare_api_token",
                "stripe_test_key",
                "smtp2go_api_key",
                "approved_test_email",
                "test_sender",
            }
            if "fulfilment" in self.config:
                required.add("fulfilment_resources")
            mailing = self.has_post_purchase
            provider = credential_provider or self.post_purchase_email_provider
            if self.has_post_purchase:
                if not self.config["reviews_enabled"] or self.consolidated:
                    required.remove("smtp2go_api_key")
                required.add(PROVIDER_KEYS[provider])
            if not mailing:
                required -= {"smtp2go_api_key", "approved_test_email", "test_sender"}
            if not required.issubset(credentials) or any(
                not isinstance(credentials[key], str) or not credentials[key] for key in required
            ):
                raise ValueError("credential handoff is incomplete; use the documented private JSON schema")
            if "fulfilment" in self.config:
                try:
                    resource_config = json.loads(credentials["fulfilment_resources"])
                    if len(credentials["fulfilment_resources"].encode()) > 65536:
                        raise ValueError("invalid resources")
                    validate_resource_configuration(resource_config, self.config["fulfilment"]["buckets"])
                except (ValueError, TypeError) as error:
                    raise ValueError("fulfilment resources must be a protected JSON object") from error
            if not credentials["stripe_test_key"].startswith(("sk_test_", "rk_test_")):
                raise ValueError("only a Stripe test credential is accepted")
            if mailing:
                validate_email_credential(credentials, provider)
                if self.config["reviews_enabled"] and not self.consolidated and provider != "smtp2go":
                    validate_email_credential(credentials, "smtp2go")
            for field in ("approved_test_email", "test_sender") if mailing else ():
                if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}", credentials[field]):
                    raise ValueError(f"invalid approved test mailbox field {field}")
        self.api = api or (API(self.config["account_id"], credentials["cloudflare_api_token"]) if credentials else None)

    @staticmethod
    def gel_inputs(manifest, assets, content_path):
        """Read only declared local images; reject escapes before creating inventory."""
        gel = json.loads(manifest.read_text())
        content = json.loads(content_path.read_text())
        references = [value for role, value in gel.get("assets", {}).items() if role != "hero_alt"]
        references.extend(product["image"] for product in content.get("products", []))
        for product in content.get("products", []):
            if "images" not in product:
                continue
            images = product["images"]
            if not isinstance(images, list) or not 1 <= len(images) <= 8:
                raise ValueError("product images must contain 1 to 8 images")
            seen = set()
            for image in images:
                if not isinstance(image, dict) or set(image) != {"url", "alt"}:
                    raise ValueError("invalid product image fields")
                if not isinstance(image["alt"], str) or not image["alt"].strip() or len(image["alt"]) > 2000:
                    raise ValueError("invalid product image alt")
                name = image["url"]
                if not isinstance(name, str) or name in seen:
                    raise ValueError("invalid or duplicate product image")
                seen.add(name)
                references.append(name)
            if images[0]["url"] != product["image"]:
                raise ValueError("product images must start with the primary image")
        for name in references:
            if not isinstance(name, str) or not re.fullmatch(r"assets/[A-Za-z0-9_./-]+\.(svg|png|jpg|jpeg|webp)", name):
                raise ValueError("GEL image references must be local assets/ paths")
            relative = Path(name)
            if ".." in relative.parts:
                raise ValueError("GEL image references must not traverse directories")
            path = assets
            for component in relative.parts:
                path = path / component
                if path.is_symlink():
                    raise ValueError("GEL image references must not use symlinks")
            if not path.is_file() or not path.resolve().is_relative_to(assets):
                raise ValueError(f"GEL image reference is missing or unsafe: {name}")
        return gel, content, set(references)

    @classmethod
    def plan(cls, directory, config):
        directory = Path(directory).resolve()
        if directory.exists() and any(p.name != ".operator.lock" for p in directory.iterdir()):
            raise ValueError("plan requires an empty new directory; resume existing inventory instead")
        config = validate_config(config)
        lock = json.loads(Path(config["preflight_lock"]).read_text())
        if lock.get("stripe_mode") != "test" or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", lock.get("catalogue_version", "")
        ):
            raise ValueError("a compiled Stripe test preflight lock is required")
        if "gel" in config:
            manifest = Path(config["gel"]).resolve(strict=True)
            assets = Path(config.get("gel_assets", manifest.parent.parent)).resolve(strict=True)
            if not manifest.is_file() or not assets.is_dir():
                raise ValueError("GEL manifest must be a file and asset root must be a directory")
            if directory.is_relative_to(assets) or assets.is_relative_to(directory):
                raise ValueError("installation directory and GEL asset root must not overlap")
            content_path = Path(config.get("theme_content", ROOT / "examples/gel-theme-pack/content/squirrel.json"))
            gel, content, references = cls.gel_inputs(manifest, assets, content_path)
        if "merchant_inputs" in config:
            source = Path(config["merchant_inputs"])
            if source.is_symlink():
                raise ValueError("merchant inputs root must not be a symlink")
            source = source.resolve(strict=True)
            if directory.is_relative_to(source) or source.is_relative_to(directory):
                raise ValueError("installation and merchant inputs must not overlap")
            validate_merchant_inputs(source, ROOT)
        if "post_purchase_config" in config:
            action_file = Path(config["post_purchase_config"])
            resolved = action_file.resolve()
            if directory.is_relative_to(ROOT) or resolved.is_relative_to(ROOT):
                raise ValueError("protected post-purchase input and installation must be outside the worktree")
            if resolved.is_relative_to(source):
                raise ValueError("protected post-purchase input must not enter the merchant package or storefront")
            raw = read_private(action_file, raw=True)
            actions = cls.parse_post_purchase_snapshot(raw, config, source, lock)
        atomic_json(directory / "inputs/preflight-lock.json", lock)
        config["preflight_lock"] = "inputs/preflight-lock.json"
        if "merchant_inputs" in config:
            destination = directory / "inputs/merchant"
            try:
                config["merchant_inputs_sha256"] = snapshot_merchant_inputs(source, destination, ROOT)
            except Exception:
                shutil.rmtree(directory / "inputs", ignore_errors=True)
                raise
            config["merchant_inputs"] = "inputs/merchant"
        if "post_purchase_config" in config:
            destination = directory / "inputs/post-purchase.json"
            atomic_json(destination, actions if isinstance(actions, dict) else {"schema": 1, "actions": actions})
            config["post_purchase_config"] = "inputs/post-purchase.json"
            config["post_purchase_config_sha256"] = digest(read_private(destination, raw=True))
        if "gel" in config:
            destination = directory / "inputs/gel-assets"
            for name in sorted(references):
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(assets / name, target)
            atomic_json(directory / "inputs/gel.json", gel)
            config["gel"] = "inputs/gel.json"
            config["gel_assets"] = "inputs/gel-assets"
            if "theme_content" in config:
                atomic_json(directory / "inputs/theme-content.json", content)
                config["theme_content"] = "inputs/theme-content.json"
        atomic_json(
            directory / "inventory.json",
            {
                "format": 2,
                "installation": config["namespace"],
                "environment": "test",
                "config": config,
                "phase": "planned",
                "resources": {},
                "pending": [],
                "releases": [],
                "secret_fingerprints": {},
                "activation_authorized": False,
            },
        )
        return cls(directory)

    @property
    def secret_names(self):
        return ('invitation', 'reviewer', 'admin', 'settlement', 'delivery', 'unsubscribe',
                'recipient_hash', 'order_access')

    @property
    def roles(self):
        return ('source', *(('reviews',) if self.config['reviews_enabled'] else ()),
                *(('post-purchase',) if self.has_post_purchase else ()))

    @staticmethod
    def parse_post_purchase_snapshot(raw, config, merchant_inputs, lock):
        if len(raw) > 32768:
            raise ValueError("protected post-purchase input is too large")
        def unique_keys(pairs):
            value = dict(pairs)
            if len(value) != len(pairs):
                raise ValueError("duplicate protected post-purchase configuration field")
            return value

        try:
            value = json.loads(raw, object_pairs_hook=unique_keys)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid protected post-purchase JSON") from error
        return validate_post_purchase(value, config, eligible_offers(merchant_inputs, lock))

    def post_purchase_actions(self):
        path = Path(self.config["post_purchase_config"])
        raw = read_private(path, raw=True)
        if digest(raw) != self.config["post_purchase_config_sha256"]:
            raise ValueError("post-purchase input changed; restore the protected snapshot")
        return self.parse_post_purchase_snapshot(
            raw, self.config, Path(self.config["merchant_inputs"]),
            json.loads(Path(self.config["preflight_lock"]).read_text()),
        )

    @property
    def queues(self):
        return ()

    def save(self):
        atomic_json(self.state_path, self.state)

    def name(self, role):
        return self.config["namespace"] + "-" + role

    def vault(self, create=False):
        if self.vault_path.exists():
            value = read_private(self.vault_path)
            expected = self.state["secret_fingerprints"]
            if not expected and create and not self.state["resources"] and self.state["phase"] == "planned":
                # A crash may occur after the private vault rename and before its
                # public fingerprints are saved. No resources can yet use these keys.
                required = {*self.secret_names, "signing_private", "signing_public", *self.post_purchase_vault_fields}
                if set(value) != required or any(not isinstance(item, str) or not item for item in value.values()):
                    raise ValueError("incomplete initial vault; restore its original backup")
                self.state["secret_fingerprints"] = {key: digest(item.encode()) for key, item in value.items()}
                self.save()
                expected = self.state["secret_fingerprints"]
            if not expected or any(
                digest(value.get(key, "").encode()) != checksum for key, checksum in expected.items()
            ):
                raise ValueError("vault identity changed; restore original vault, never regenerate or rotate keys")
            if not set(self.post_purchase_vault_fields).issubset(expected):
                raise ValueError("post-purchase vault keys are missing from the recorded fingerprints")
            return value
        if not create or self.state["secret_fingerprints"] or self.state["resources"]:
            raise ValueError("installation vault missing; restore its original backup before continuing")
        values = {name: secrets.token_hex(32) for name in self.secret_names}
        if self.has_post_purchase:
            values.update(self.initial_post_purchase_secrets())
        generated = subprocess.run(
            [
                "node",
                "-e",
                "const c=require('node:crypto');const k=c.generateKeyPairSync('ed25519');"
                "console.log(JSON.stringify({private:k.privateKey.export({type:'pkcs8',format:'der'}).toString('base64'),"
                "public:k.publicKey.export({type:'spki',format:'der'}).toString('base64')}))",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pair = json.loads(generated.stdout)
        values.update(signing_private=pair["private"], signing_public=pair["public"])
        atomic_json(self.vault_path, values)
        self.state["secret_fingerprints"] = {name: digest(value.encode()) for name, value in values.items()}
        self.save()
        return values

    def assert_resources(self):
        for key, resource in self.state["resources"].items():
            kind, role = key.split(":", 1)
            if resource["name"] != self.name(role) and kind != "consumer":
                raise ValueError(f"{key}: inventory resource name differs from namespace")
            if kind == "database":
                actual = self.api.request("d1/database/" + resource["id"])
                if actual["name"] != self.name(role):
                    raise ValueError(f"{key}: remote database identity differs; no migration/deploy will run")
            elif kind == "queue":
                actual = self.api.request("queues/" + resource["id"])
                if actual["queue_name"] != self.name(role):
                    raise ValueError(f"{key}: remote queue identity differs")

    def provider_ready(self):
        if not self.api or not self.credentials:
            raise ValueError("this operation requires --credentials with documented test credentials")
        key = self.credentials.get("stripe_test_key", "")
        if not key.startswith(("sk_test_", "rk_test_")):
            raise ValueError("live or missing Stripe credentials are not accepted")
        if stripe(key, "account").get("id") != self.config["stripe_account_id"]:
            raise ValueError("Stripe credential account mismatch")

    def provision(self):
        self.provider_ready()
        self.assert_resources()
        self.vault(create=True)
        self.check_post_purchase_handoff()
        account = self.api.request("workers/subdomain")["subdomain"]
        self.state["workers_subdomain"] = account
        self.save()
        for kind, roles, endpoint, name_field, identity in [
            ("database", DATABASES, "d1/database", "name", "uuid"),
            ("queue", self.queues, "queues", "queue_name", "queue_id"),
        ]:
            for role in roles:
                key = kind + ":" + role
                if key in self.state["resources"]:
                    continue
                matches = [item for item in self.api.listing(endpoint) if item.get(name_field) == self.name(role)]
                if len(matches) > 1 or (matches and key not in self.state["pending"]):
                    raise ValueError(f"unmanaged/colliding resource {self.name(role)}; select a fresh namespace")
                if not matches:
                    if key not in self.state["pending"]:
                        self.state["pending"].append(key)
                        self.save()
                    result = self.api.request(endpoint, "POST", {name_field: self.name(role)})
                else:
                    result = matches[0]
                self.state["resources"][key] = {"name": self.name(role), "id": result[identity]}
                self.state["pending"].remove(key)
                self.save()
        self.provision_workers()
        self.provision_webhook()
        self.provision_pages()
        if self.state["phase"] == "planned":
            self.state["phase"] = "provisioned"
        self.save()
        return self.state["resources"]

    def provision_workers(self):
        existing = {item["id"] for item in self.api.listing("workers/scripts")}
        for role in self.roles:
            key = "worker:" + role
            if key in self.state["resources"]:
                continue
            name = self.name(role)
            if name in existing and key not in self.state["pending"]:
                raise ValueError(f"unmanaged Worker {name}; choose a fresh namespace")
            if name not in existing:
                if key not in self.state["pending"]:
                    self.state["pending"].append(key)
                    self.save()
                self.api.upload(
                    name,
                    [
                        (
                            "bootstrap.mjs",
                            b"export default {fetch(){return new Response(null,{status:503})}}",
                            "application/javascript+module",
                        )
                    ],
                    {"main_module": "bootstrap.mjs", "bindings": [], "compatibility_date": "2026-08-31"},
                    create=True,
                )
            self.set_worker_visibility(role, False)
            self.state["resources"][key] = {"name": name, "id": name}
            self.state["pending"].remove(key)
            self.save()

    def provision_webhook(self):
        vault = self.vault()
        if "webhook_secret" in vault:
            if "webhook" not in self.state:
                endpoint = vault.get("webhook_endpoint")
                if not endpoint:
                    raise ValueError("webhook endpoint receipt missing; restore inventory before continuing")
                self.state["webhook"] = endpoint
                self.save()
            return
        if "webhook" in self.state:
            raise ValueError("webhook secret is missing; restore the vault, never recreate the endpoint")
        if "webhook_pending_at" in self.state and time.time() - self.state["webhook_pending_at"] > 23 * 3600:
            raise ValueError(
                "unknown webhook create outcome exceeds Stripe idempotency window; inspect endpoint and restore vault"
            )
        self.state.setdefault("webhook_pending_at", time.time())
        self.save()
        url = self.config.get("webhook_url") or f"https://{self.name('source')}.{self.state['workers_subdomain']}.workers.dev/stripe/webhook"
        result = stripe(
            self.credentials["stripe_test_key"],
            "webhook_endpoints",
            {
                "url": url,
                "enabled_events[0]": "checkout.session.completed",
                "enabled_events[1]": "checkout.session.expired",
                "enabled_events[2]": "checkout.session.async_payment_succeeded",
                "api_version": "2025-09-30.clover",
            },
            idempotency=self.name("webhook-v1"),
        )
        vault["webhook_secret"] = result["secret"]
        vault["webhook_endpoint"] = {"id": result["id"], "url": url}
        atomic_json(self.vault_path, vault)
        # Only the non-regenerable installation keys are fingerprinted; provider webhook
        # creation uses a stable Stripe idempotency key and is journalled separately.
        self.state["webhook"] = {"id": result["id"], "url": url}
        self.save()

    def provision_pages(self):
        if "storefront_origin" in self.config:
            return  # Existing site routing/assets are owned by its independent operator.
        key, name = "pages:store", self.name("store")
        if key in self.state["resources"]:
            self.ensure_pages_subdomain()
            return
        matches = [item for item in self.api.listing("pages/projects") if item["name"] == name]
        if matches and key not in self.state["pending"]:
            raise ValueError("unmanaged Pages project; choose a fresh namespace")
        if not matches:
            if key not in self.state["pending"]:
                self.state["pending"].append(key)
                self.save()
            project = self.api.request("pages/projects", "POST", {"name": name, "production_branch": "main"})
        else:
            project = matches[0]
        self.state["resources"][key] = {"name": name, "id": name}
        self.state["pending"].remove(key)
        self.save()
        self.ensure_pages_subdomain(project)

    def ensure_pages_subdomain(self, project=None):
        """Retain the provider-assigned origin, including collision suffixes."""
        if "storefront_origin" in self.config:
            return
        if self.state.get("pages_subdomain"):
            return
        project = project or {}
        if not project.get("subdomain"):
            project = self.api.request("pages/projects/" + self.name("store"))
        domain = project.get("subdomain")
        if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.pages\.dev", domain):
            raise CloudError("Pages project has no valid assigned pages.dev subdomain; retry provision")
        self.state["pages_subdomain"] = domain
        self.save()

    def run(self, step, command, environment=None):
        logs = self.directory / "logs"
        logs.mkdir(exist_ok=True)
        output = logs / (step + ".log")
        with output.open("wb") as stream:
            os.chmod(output, 0o600)
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise CloudError(f"{step} failed (exit {result.returncode}); inspect protected {output}, then resume")

    def private(self, service, path, body=None, token=None, method="POST"):
        bindings = self.directory / "operator-bindings.toml"
        names = {"REVIEWS": "reviews"}
        lines = [
            f'name = "{self.name("operator-local")}"',
            f'account_id = "{self.config["account_id"]}"',
            'compatibility_date = "2026-08-31"',
        ]
        for binding, role in names.items():
            if role not in self.roles:
                continue
            lines += ["[[services]]", f'binding = "{binding}"', f'service = "{self.name(role)}"', "remote = true"]
        bindings.write_text("\n".join(lines) + "\n")
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = "Bearer " + token
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("bindings.mjs")), str(bindings)],
            input=json.dumps(
                {
                    "service": service,
                    "path": path,
                    "method": method,
                    "headers": headers,
                    **({"body": body} if body is not None else {}),
                }
            ),
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
            env={
                **os.environ,
                "CLOUDFLARE_API_TOKEN": self.credentials["cloudflare_api_token"],
                "WRANGLER_LOG": "error",
            },
        )
        if result.returncode:
            log = self.directory / "private-service-error.log"
            log.write_text(result.stdout + result.stderr)
            log.chmod(0o600)
            raise CloudError(f"private service operation failed; inspect protected {log}, then retry")
        payload = json.loads([line for line in result.stdout.splitlines() if line.startswith("{")][-1])
        if payload["status"] >= 400:
            raise CloudError(f"private {service} {method} {path}: HTTP {payload['status']}")
        return json.loads(payload["body"]) if payload["body"] else {}

    def check_schema(self):
        self.assert_resources()
        namespaces = self.api.query(self.db("commerce"), "SELECT DISTINCT namespace FROM cartray_operator_migrations")
        if {row["namespace"] for row in namespaces} != set(self.migrations()):
            raise ValueError("unknown or missing migration namespace")
        for role, expected in self.state.get("migration_ledgers", {}).items():
            actual = self.api.query(
                self.db(role),
                "SELECT position,path,checksum FROM cartray_operator_migrations WHERE namespace=? ORDER BY position",
                [role],
            )
            if actual != expected:
                raise ValueError(f"{role}: live migration ledger differs; no application rollback/deploy is permitted")
        if set(self.state.get("migration_ledgers", {})) != set(self.migrations()):
            raise ValueError("all configured migration ledgers are required")

    def upgrade(self):
        if self.state.get("post_purchase_provider_migration"):
            raise ValueError("finish credential reconfiguration and OFF deployment before upgrade")
        current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if self.state["phase"] in ("active", "paused") and self.state["deployment"]["commit"] == current:
            return self.doctor()
        self.require_post_purchase_off()
        was_active = self.state["activation_authorized"]
        stay_paused = (self.state.get("automation_paused", False)
                       or self.state.get("reviews_explicit_activation_required", False))
        self.migrate()
        self.build()
        self.deploy()
        if was_active and not stay_paused:
            return self.activate(True)
        result = self.verify()
        if was_active and stay_paused:
            self.verifier("record")
            self.state["phase"] = "paused"
            self.save()
            return {"phase": "paused", "activation_watermark": self.state.get("activation_watermark")}
        return result

    def render(self, versions=None, sending=False, post_purchase_modes=None):
        self.ensure_pages_subdomain()
        nodes, urls = graph(self, versions, sending, post_purchase_modes)
        configs = self.directory / "configs"
        configs.mkdir(exist_ok=True)
        for role, node in nodes.items():
            (configs / (role + ".toml")).write_text(wrangler_config(self, role, node))
        self.state["urls"] = urls
        self.save()
        return nodes

    def verify_merchant_inputs(self):
        if "merchant_inputs" not in self.config:
            return
        source = Path(self.config["merchant_inputs"])
        if validate_merchant_inputs(source, ROOT) != self.config.get("merchant_inputs_sha256"):
            raise ValueError("merchant inputs changed; restore the recorded snapshot before building or deploying")
        site = source / "storefront"
        storefront = javascript_json(site / "storefront-config.js", "window.CARTRAY_STOREFRONT = ")
        if storefront.get("storeName") != self.config["merchant_name"]:
            raise ValueError("merchant storefront identity differs from installation")
        cart = ConnectedShell()
        cart.feed((site / "cart/index.html").read_text())
        if "theme-" + self.config["theme"] not in cart.body.get("class", "").split():
            raise ValueError("merchant storefront theme differs from installation")
        if not set(self.config["subject_map"]).issubset(storefront["productPaths"]):
            raise ValueError("review subject mapping refers to an unauthorised commerce product")
        prefixes = {}
        for route in storefront["productPaths"].values():
            register_path(route, prefixes)
        for subject, route, _label in self.config["subject_details"]:
            register_path(route, prefixes)
            product = next((k for k, v in self.config["subject_map"].items() if v == subject), None)
            if product is not None and storefront["productPaths"][product] != route:
                raise ValueError("review subject route differs from its merchant product route")
            if not (site / route.lstrip("/") / "index.html").is_file():
                raise ValueError("review subject route is missing from the merchant storefront")
        if self.config["reviews_enabled"]:
            approved_routes = {route: subject for subject, route, _label in self.config["subject_details"]}
            # Product and non-checkout listing routes can live outside /products/.
            for product_page in site.rglob("index.html"):
                page = ConnectedShell()
                page.feed(product_page.read_text())
                if not page.product_data:
                    continue
                try:
                    data = json.loads(page.product_data)
                    if not isinstance(data, dict):
                        continue
                    actual = data.get("review_subject")
                except (ValueError, AttributeError) as error:
                    raise ValueError("merchant product review presentation is invalid") from error
                route = "/" + str(product_page.parent.relative_to(site)) + "/"
                if actual is not None and approved_routes.get(route) != actual:
                    raise ValueError("merchant product contains an unapproved review subject")
            for subject, route, _label in self.config["subject_details"]:
                page = ConnectedShell()
                page.feed((site / route.lstrip("/") / "index.html").read_text())
                try:
                    actual = json.loads(page.product_data).get("review_subject")
                except (ValueError, AttributeError) as error:
                    raise ValueError("merchant product review presentation is missing") from error
                if (actual != subject or not {"reviews", "reviews-status", "review-list", "reviews-more"} <= page.ids
                        or "/theme/connected-public-reviews.mjs" not in page.module_scripts):
                    raise ValueError("merchant product review presentation differs from subject mapping")
            for route in ("review/index.html", "review/manage.html"):
                if not (site / route).is_file():
                    raise ValueError("merchant review and management pages are required")
                shell = ConnectedShell()
                shell.feed((site / route).read_text())
                mode = "manage" if route.endswith("manage.html") else "review"
                if (mode not in shell.review_modes or not {"content", "message", "confirm-dialog"} <= shell.ids
                        or "/theme/review-presenter.mjs" not in shell.module_scripts
                        or (mode == "manage" and not {"data-dialog-cancel", "data-dialog-confirm"}
                            <= shell.dialog_controls)):
                    raise ValueError("merchant review page does not load the supported review client")
            for dest, source in {"theme/native-reviews.js": "modules/reviews/ui/reviews.js",
                                 "theme/native-manage.js": "modules/reviews/ui/manage.js",
                                 "theme/review-presenter.mjs": "examples/gel-theme-pack/src/connected-review.mjs",
                                 "theme/connected-public-reviews.mjs":
                                     "examples/gel-theme-pack/src/connected-public-reviews.mjs",
                                 "theme/reviews.mjs": "modules/reviews/ui/public.mjs"}.items():
                if (site / dest).read_bytes() != (ROOT / source).read_bytes():
                    raise ValueError("merchant review client differs from the supported Reviews module")

    def update_merchant_inputs(self, source):
        """Explicit local input refresh while sending is OFF; never changes keys or storage."""
        self.require_post_purchase_off()
        if self.state.get('activation_authorized') and not self.state.get('automation_paused'):
            raise ValueError('pause sending before replacing merchant build inputs')
        if 'merchant_inputs' not in self.config or self.state.get('post_purchase_transition'):
            raise ValueError('a paused merchant installation is required to replace build inputs')
        source = Path(source).resolve(strict=True)
        checksum = validate_merchant_inputs(source, ROOT)
        destination = self.directory / 'inputs' / ('merchant-' + checksum)
        try:
            retained = destination.exists() and validate_merchant_inputs(destination, ROOT) == checksum
        except (ValueError, FileNotFoundError):
            retained = False
        if not retained:
            with tempfile.TemporaryDirectory(prefix='merchant-copy-', dir=destination.parent) as temporary:
                candidate = Path(temporary) / 'snapshot'
                if snapshot_merchant_inputs(source, candidate, ROOT) != checksum:
                    raise ValueError('merchant source changed while being copied')
                if destination.exists():
                    # Preserve an interrupted local copy for diagnosis. No
                    # database, key, source package or release is deleted.
                    os.replace(destination, destination.with_name(
                        destination.name + '-interrupted-' + str(time.time_ns())))
                os.replace(candidate, destination)
        old = dict(self.config)
        self.config.update(merchant_inputs=str(destination), merchant_inputs_sha256=checksum)
        try:
            self.verify_merchant_inputs()
        except Exception:
            self.config = old
            raise
        self.state['config'].update(merchant_inputs=str(destination.relative_to(self.directory)),
                                    merchant_inputs_sha256=checksum)
        self.save()

    def merchant_config_hash(self):
        if "merchant_inputs" not in self.config:
            return None
        fields = ("store_key", "subject_map", "subject_details", "merchant_name", "theme", "reviews_enabled",
                  "review_invitation_default_checked")
        identity = {key: self.config[key] for key in fields}
        identity.update({key: self.config[key] for key in ("storefront_origin", "success_path", "cancel_path",
                                                         "webhook_url", "source_mode", "source_api_path",
                                                         "post_purchase_config_sha256", "fulfilment", "review_requests")
                         if key in self.config})
        if "post_purchase_email_provider" in self.config:
            identity["post_purchase_email_provider"] = self.config["post_purchase_email_provider"]
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return digest(encoded.encode())

    def build(self):
        if self.state.get("post_purchase_provider_migration", {}).get("status") == "staging":
            raise ValueError("resume credential reconfiguration before building")
        self.vault()
        self.check_schema()
        self.verify_merchant_inputs()
        if self.has_post_purchase:
            self.post_purchase_actions()
        merchant_lock_hash = (digest(Path(self.config["preflight_lock"]).read_bytes())
                              if "merchant_inputs" in self.config else None)
        merchant_config_hash = self.merchant_config_hash()
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT, check=True)
        release = self.directory / "releases" / commit
        if (release / "build.json").exists():
            recorded = json.loads((release / "build.json").read_text())
            if (recorded.get("merchant_inputs_sha256") != self.config.get("merchant_inputs_sha256")
                    or recorded.get("merchant_lock_sha256") != merchant_lock_hash
                    or recorded.get("merchant_config_sha256") != merchant_config_hash):
                raise ValueError("cached release uses different merchant inputs; build a new release")
            if self.has_post_purchase:
                self.check_post_purchase_release(recorded)
            self.state["build"] = commit
            self.save()
            return recorded
        release.mkdir(parents=True, exist_ok=True)
        self.render(post_purchase_modes=OFF)
        source = release / "source"
        if "merchant_inputs" in self.config and (source / "receipt.json").exists():
            receipt = json.loads((source / "receipt.json").read_text())
            expected_inputs = commerce_build_hash(
                Path(self.config["merchant_inputs"]) / "commerce", Path(self.config["preflight_lock"]).read_bytes()
            )
            if (receipt.get("source_commit") != commit or receipt.get("catalogue_lock_sha256") != merchant_lock_hash
                    or receipt.get("merchant_input_sha256") != expected_inputs):
                if source.is_symlink():
                    raise ValueError("refusing symlinked generated source artifact")
                shutil.rmtree(source)
        if not (source / "receipt.json").exists():
            if source.exists():
                shutil.rmtree(source)
            self.run(
                "build-source",
                [
                    "uv",
                    "run",
                    "--locked",
                    "python",
                    "scripts/build_acorn_commerce.py",
                    "--preflight-lock",
                    self.config["preflight_lock"],
                    "--wrangler-config",
                    str(self.directory / "configs/source.toml"),
                    "--output",
                    str(source),
                    *(["--merchant-input", str(Path(self.config["merchant_inputs"]) / "commerce")]
                      if "merchant_inputs" in self.config else []),
                ],
            )
        if "merchant_inputs" in self.config:
            receipt = json.loads((source / "receipt.json").read_text())
            expected_inputs = commerce_build_hash(
                Path(self.config["merchant_inputs"]) / "commerce", Path(self.config["preflight_lock"]).read_bytes()
            )
            if (receipt.get("source_commit") != commit or receipt.get("catalogue_lock_sha256") != merchant_lock_hash
                    or receipt.get("merchant_input_sha256") != expected_inputs):
                raise ValueError("built commerce artifact does not match the pinned merchant inputs and lock")
        reviews = release / "reviews"
        if self.config["reviews_enabled"] and not (reviews / "receipt.json").exists():
            if reviews.exists():
                shutil.rmtree(reviews)
            self.run(
                "build-reviews",
                [
                    "uv",
                    "run",
                    "--locked",
                    "python",
                    "modules/reviews/tools/clean_qualification_build.py",
                    "--output",
                    str(reviews),
                ],
            )
        nodes, _ = graph(self)
        artifacts = {}
        for role, node in nodes.items():
            if role in ("source", "reviews"):
                directory = release / role / "bundle"
                files = {
                    str(item.relative_to(directory)): digest(item.read_bytes())
                    for item in directory.rglob("*")
                    if item.is_file() and item.name not in ("README.md", "wrangler.toml")
                }
            else:
                output = release / (role + ".mjs")
                self.run(
                    "build-" + role,
                    [
                        str(ROOT / "node_modules/.bin/esbuild"),
                        str(ROOT / node["main"]),
                        "--bundle",
                        "--format=esm",
                        "--outfile=" + str(output),
                    ],
                )
                files = {role + ".mjs": digest(output.read_bytes())}
            artifacts[role] = files
        result = {
            "commit": commit,
            "merchant_inputs_sha256": self.config.get("merchant_inputs_sha256"),
            "merchant_lock_sha256": merchant_lock_hash,
            "merchant_config_sha256": merchant_config_hash,
            "artifacts": artifacts,
            "schema": self.state.get("migration_ledgers"),
            "suppression_key_fingerprint": self.state["secret_fingerprints"]["recipient_hash"],
        }
        if self.has_post_purchase:
            result.update(
                post_purchase_config_sha256=self.config["post_purchase_config_sha256"],
                post_purchase_vault_fingerprints={
                    key: self.state["secret_fingerprints"][key] for key in self.post_purchase_vault_fields
                },
            )
        atomic_json(release / "build.json", result)
        self.state["build"] = commit
        self.save()
        return result

    def artifact_modules(self, role, build):
        release = self.directory / "releases" / build["commit"]
        folder = release / role / "bundle" if role in ("source", "reviews") else release
        modules = []
        for name, expected in build["artifacts"][role].items():
            value = (folder / name).read_bytes()
            if digest(value) != expected:
                raise ValueError(f"retained artifact changed: {role}/{name}")
            kind = (
                "text/x-python"
                if name.endswith(".py")
                else ("application/javascript+module" if name.endswith(".mjs") else "application/octet-stream")
            )
            modules.append((name, value, kind))
        return modules

    def set_worker_visibility(self, role, public):
        self.api.request(
            f"workers/scripts/{self.name(role)}/subdomain",
            "POST",
            {"enabled": public, "previews_enabled": False},
        )

    def upload_worker(self, role, node, build):
        python = role in ("source", "reviews")
        metadata = {
            "main_module": "worker.py" if python else role + ".mjs",
            "bindings": node["bindings"] + secret_bindings(self, role),
            "compatibility_date": "2026-08-31",
            "compatibility_flags": (
                ["python_workers"] + (["disable_python_external_sdk"] if role == "reviews" else [])
            ) if python else [],
            "annotations": {"workers/message": "CartRay test operator " + build["commit"]},
        }
        return self.api.upload(self.name(role), self.artifact_modules(role, build), metadata)

    def deploy(self):
        if self.state.get("post_purchase_provider_migration", {}).get("status") == "staging":
            raise ValueError("resume credential reconfiguration before deployment")
        self.require_post_purchase_off()
        self.ensure_pages_subdomain()
        self.check_schema()
        self.verify_merchant_inputs()
        self.pin_suppression()
        if "build" not in self.state:
            raise ValueError("build the clean commit before deployment")
        build = json.loads((self.directory / "releases" / self.state["build"] / "build.json").read_text())
        if build.get("merchant_inputs_sha256") != self.config.get("merchant_inputs_sha256"):
            raise ValueError("release merchant inputs differ; rebuild before deploying")
        if build.get("merchant_config_sha256") != self.merchant_config_hash():
            raise ValueError("release merchant configuration differs; rebuild before deploying")
        if ("merchant_inputs" in self.config
                and build.get("merchant_lock_sha256") != digest(Path(self.config["preflight_lock"]).read_bytes())):
            raise ValueError("release preflight lock differs; rebuild before deploying")
        if build["schema"] != self.state.get("migration_ledgers"):
            raise ValueError("build schema contract differs; migrate then build a new release")
        if self.has_post_purchase:
            self.post_purchase_actions()
            self.check_post_purchase_release(build)
            self.check_post_purchase_handoff()
            self.provider_ready()
            self.check_current_post_purchase_off()
        progress = self.state.setdefault("deployment", {"commit": build["commit"], "versions": {}, "complete": False})
        if progress["commit"] != build["commit"]:
            if not progress.get("complete"):
                raise ValueError("finish or roll back the interrupted deployment before another release")
            progress = {"commit": build["commit"], "versions": {}, "complete": False}
            self.state["deployment"] = progress
        # Validate every retained artifact before any runtime mutation.
        for role in self.roles:
            self.artifact_modules(role, build)
        progress["sending"] = False
        self.state["phase"] = "deploying"
        self.save()
        for role in (
            role
            for role in (
                "post-purchase", "source", "reviews"
            )
            if role in self.roles
        ):
            nodes, _ = graph(self, progress["versions"], sending=False, post_purchase_modes=OFF)
            node = nodes[role]
            if role in progress["versions"]:
                self.set_worker_visibility(role, node.get("public", False))
                continue
            if not node.get("public", False):
                self.set_worker_visibility(role, False)
            result = self.upload_worker(role, node, build)
            self.api.publish(self.name(role), result["id"])
            progress["versions"][role] = result["id"]
            self.save()
            self.set_worker_visibility(role, node.get("public", False))
        self.api.request(f"workers/scripts/{self.name('source')}/schedules", "PUT",
                         [{"cron": "0 * * * *"}] + ([{"cron":"* * * * *"}] if self.consolidated else []))
        self.render(progress["versions"])
        self.seed_subjects()
        self.deploy_storefront(build)
        if self.has_post_purchase:
            for role in POST_PURCHASE_ROLES:
                self.check_post_purchase_version(role, "disabled", self.api.version(self.name(role)),
                                                 progress["versions"][role])
                errors = self.post_purchase_visibility_errors(role)
                if errors:
                    raise ValueError("; ".join(errors))
            self.record_post_purchase_off(build)
        self.state["deployment"]["complete"] = True
        migration = self.state.pop("post_purchase_provider_migration", None)
        if migration:
            self.state.setdefault("post_purchase_provider_migrations", []).append({**migration, "status": "complete"})
            if self.config["reviews_enabled"]:
                self.state["reviews_explicit_activation_required"] = True
        self.state["phase"] = "deployed"
        self.save()
        return {"commit": build["commit"], "versions": progress["versions"], "activated": False}

    def seed_subjects(self):
        if not self.config["reviews_enabled"]:
            return
        subjects = self.config.get("subject_details", [
            ("acorn-growing-kit", "/products/acorn-growing-kit/", "Acorn growing kit"),
            ("woodland-planter", "/products/woodland-planter/", "Woodland planter"),
        ])
        store = self.config.get("store_key", "acorn-store")
        for subject, path, label in subjects:
            self.api.query(
                self.db("reviews"),
                "INSERT INTO review_subjects VALUES (?,'test',?,'product',?) ON CONFLICT DO NOTHING",
                [store, subject, path],
            )
            self.api.query(
                self.db("reviews"),
                "INSERT INTO review_subject_display_names VALUES (?,'test',?,?) ON CONFLICT DO NOTHING",
                [store, subject, label],
            )
            self.api.query(
                self.db("reviews"),
                "INSERT INTO native_publication_versions "
                "(store_key,environment,subject_id,version,contribution_count,rated_count,rating_sum) "
                "VALUES (?,'test',?,0,0,0,0) ON CONFLICT DO NOTHING",
                [store, subject],
            )

    def deploy_storefront(self, build):
        output = self.directory / "releases" / build["commit"] / "storefront"
        integrity = output.parent / "storefront-integrity.json"
        if output.is_symlink() or integrity.is_symlink():
            raise ValueError("refusing symlinked generated storefront or integrity record")
        if ("merchant_inputs" in self.config and (output / ".operator-build-complete").exists()
                and not integrity.exists()):
            shutil.rmtree(output)  # Resume an interrupted local generation from the pinned snapshot.
        if not (output / ".operator-build-complete").exists():
            staging = output.with_name("storefront.partial")
            if staging.exists():
                shutil.rmtree(staging)
            if output.exists():
                shutil.rmtree(output)
            if "merchant_inputs" in self.config:
                site = Path(self.config["merchant_inputs"]) / "storefront"
                shutil.copytree(site, staging)
                storefront = javascript_json(site / "storefront-config.js", "window.CARTRAY_STOREFRONT = ")
                storefront["reviewsEnabled"] = self.config["reviews_enabled"]
                storefront["reviewInvitationsEnabled"] = (self.config["reviews_enabled"] and
                    self.config.get("review_requests", {}).get("timing", "receipt") != "off")
                storefront["reviewInvitationDefaultChecked"] = (
                    self.config["reviews_enabled"] and self.config["review_invitation_default_checked"]
                )
                from cartray.store import review_invitation_disclosure
                version, wording = review_invitation_disclosure(
                    2 if self.config.get('review_requests', {}).get('timing') == 'both' else 1)
                storefront['reviewInvitationWordingVersion'] = version
                storefront['reviewInvitationWording'] = wording
                storefront["reviewSubjects"] = self.config["subject_map"] if self.config["reviews_enabled"] else {}
                theme = javascript_json(site / "site-config.js", "window.CARTRAY_THEME = ")
                theme["contextPath"] = None
                theme["returnExtension"] = None
                (staging / "storefront-config.js").write_text(
                    "window.CARTRAY_STOREFRONT = " + json.dumps(storefront, ensure_ascii=True)
                    + "; window.CARTRAY_STOREFRONT.apiBaseUrl = "
                    + json.dumps(self.state["urls"]["source"]) + "; window.CARTRAY_CONNECTED_CATALOGUE = true;\n"
                )
                (staging / "site-config.js").write_text(
                    "window.CARTRAY_THEME = " + json.dumps(theme, ensure_ascii=True) + ";\n"
                )
                if self.config["reviews_enabled"] or "fulfilment" in self.config:
                    shutil.copyfile(ROOT / "modules/reviews/gateway/worker.mjs", staging / "_worker.js")
            else:
                self.run(
                    "build-storefront",
                    [
                        "uv",
                        "run",
                        "--locked",
                        "python",
                        "examples/acorn-store/build.py",
                        "--output",
                        str(staging),
                        "--test-checkout-api",
                        self.state["urls"]["source"],
                        "--theme",
                        self.config["theme"],
                        "--gel",
                        self.config.get("gel", str(ROOT / "examples/gel-theme-pack/gel/squirrel.json")),
                        *(["--gel-assets", self.config["gel_assets"]] if "gel_assets" in self.config else []),
                        *(["--theme-content", self.config["theme_content"]] if "theme_content" in self.config else []),
                        *(["--reviews", "--test-review-gateway"] if self.config["reviews_enabled"] else []),
                        "--preflight-lock",
                        self.config["preflight_lock"],
                    ],
                )
            (staging / ".operator-build-complete").write_text(build["commit"] + "\n")
            os.replace(staging, output)
            if "merchant_inputs" in self.config:
                atomic_json(integrity, {"sha256": tree_hash(output)})
        if "merchant_inputs" in self.config:
            if not integrity.is_file() or json.loads(integrity.read_text()).get("sha256") != tree_hash(output):
                raise ValueError("retained merchant storefront changed; rebuild before deploying")
        if "storefront_origin" in self.config:
            self.state["external_storefront"] = {
                "origin": self.config["storefront_origin"], "artifact": str(output.relative_to(self.directory)),
                "sha256": tree_hash(output), "deployment": "external-operator-required",
            }
            self.save()
            return  # Never upload over an existing site's Worker/assets.
        origin = self.state["urls"]["storefront"]
        deployment = {
            "env_vars": {
                "ACORN_REVIEWS_GATEWAY": {"type": "plain_text", "value": "test"},
                "ACORN_REVIEWS_READ_MODE": {"type": "plain_text", "value": "test"},
                "ACORN_STOREFRONT_ORIGIN": {"type": "plain_text", "value": origin},
            },
            "services": {
                name: {"service": self.name("reviews"), "environment": "production"}
                for name in ("REVIEWS_API", "REVIEWS_PUBLIC")
            },
        }
        if "merchant_inputs" in self.config and self.config["reviews_enabled"]:
            deployment["env_vars"] = {
                "REVIEWS_GATEWAY": {"type": "plain_text", "value": "test"},
                "REVIEWS_READ_MODE": {"type": "plain_text", "value": "test"},
                "REVIEWS_STOREFRONT_ORIGIN": {"type": "plain_text", "value": origin},
            }
        if not self.config["reviews_enabled"]:
            deployment = {"env_vars": {}, "services": {}}
        if "fulfilment" in self.config or (self.consolidated and self.config['reviews_enabled']):
            deployment["env_vars"]["ORDER_ACCESS_GATEWAY"] = {"type": "plain_text", "value": "test"}
            deployment["env_vars"]["CARTRAY_SOURCE_API_PATH"] = {
                "type": "plain_text", "value": self.config.get("source_api_path", "")
            }
            deployment["env_vars"]["REVIEWS_STOREFRONT_ORIGIN"] = {"type": "plain_text", "value": origin}
            deployment["services"]["CARTRAY_SOURCE"] = {"service": self.name("source"), "environment": "production"}
        self.api.request(
            "pages/projects/" + self.name("store"), "PATCH", {"deployment_configs": {"production": deployment}}
        )
        self.run(
            "deploy-storefront",
            [
                str(ROOT / "node_modules/.bin/wrangler"),
                "pages",
                "deploy",
                str(output),
                "--project-name",
                self.name("store"),
                "--branch",
                "main",
            ],
            {
                **os.environ,
                "CLOUDFLARE_API_TOKEN": self.credentials["cloudflare_api_token"],
                "CLOUDFLARE_ACCOUNT_ID": self.config["account_id"],
            },
        )

    def verifier(self, mode):
        self.check_schema()
        nodes, _ = graph(self, self.state['deployment']['versions'])
        errors = []
        for role, node in nodes.items():
            errors.extend(self.worker_binding_errors(role, node, self.api.version(self.name(role)),
                          self.state['deployment']['versions'].get(role)))
        if errors:
            raise ValueError('; '.join(errors))
        # The immutable commerce installation row and existing checkpoint
        # remain runtime authority; no second subscriber or watermark.
        fence = self.post_purchase_fence() if self.has_post_purchase else {'status':'uninitialised'}
        return {'status':'active' if fence['status']=='retained' else 'uninitialised', 'revision':0}

    def verify(self):
        if self.config["reviews_enabled"]:
            visibility_errors = self.private_source_visibility_errors()
            if visibility_errors:
                raise ValueError("; ".join(visibility_errors))
        if not self.config["reviews_enabled"]:
            health = self.doctor()
            if not health["healthy"]:
                raise ValueError("commerce deployment differs; resolve doctor findings")
        self.pin_suppression()
        result = self.verifier("inspect")
        if self.state["phase"] != "active":
            self.state["phase"] = "verified"
            self.save()
        return {
            "phase": self.state["phase"],
            "activation_authorized": self.state["activation_authorized"],
            "versions": {key: value for key, value in result.items() if key.endswith("_version")},
        }

    def activate(self, enable_test_delivery=False):
        if not self.has_post_purchase:
            raise ValueError('configure the shared order-confirmation email provider before activation')
        if not enable_test_delivery:
            raise ValueError('activation requires explicit --enable-test-delivery')
        if self.state.get('post_purchase_provider_migration'):
            raise ValueError('finish credential reconfiguration and OFF deployment before activation')
        if (not self.state.get('deployment', {}).get('complete') or
                self.state.get('build') != self.state['deployment'].get('commit')):
            raise ValueError('deploy the selected build before activation')
        self.verify()
        if self.post_purchase_fence()['status'] == 'uninitialised':
            from scripts.post_purchase_installation import remote_import, validate_backend
            database, name = validate_backend(self, self.api)
            remote_import(self.config['account_id'], database, name,
                          self.credentials['cloudflare_api_token'], self.config['store_key'])
        return self.post_purchase_activate(enable_test_delivery)


    def pause(self):
        if not self.has_post_purchase:
            return {'phase':'paused', 'sending':False}
        return self.post_purchase_disable()

    def private_source_visibility_errors(self):
        if self.config.get("source_mode", "public") != "private":
            return []
        return self.worker_visibility_errors("source")

    def worker_visibility_errors(self, role):
        visibility = self.api.request(f"workers/scripts/{self.name(role)}/subdomain")
        errors = []
        for field in ("enabled", "previews_enabled"):
            if not isinstance(visibility, dict) or visibility.get(field) is not False:
                errors.append(f"{role}.visibility.{field}: must be explicitly disabled in private mode")
        return errors

    def worker_binding_errors(self, role, expected, actual, version):
        errors = []
        if actual["id"] != version:
            errors.append(f"{role}: active version differs from inventory")
        observed = {item["name"]: item for item in actual["resources"]["bindings"]}
        for binding in expected["bindings"]:
            name, kind = binding["name"], binding["type"]
            value = observed.get(name, {})
            if value.get("type") != kind:
                errors.append(f"{role}.bindings.{name}: missing/wrong type")
            elif kind == "d1" and value.get("database_id") != binding["id"]:
                errors.append(f"{role}.bindings.{name}: wrong database")
            elif kind == "r2_bucket" and value.get("bucket_name") != binding["bucket_name"]:
                errors.append(f"{role}.bindings.{name}: wrong bucket")
            elif kind == "service" and value.get("service") != binding["service"]:
                errors.append(f"{role}.bindings.{name}: wrong service")
            elif kind == "queue" and value.get("queue_name") != binding["queue_name"]:
                errors.append(f"{role}.bindings.{name}: wrong queue")
            elif kind == "ratelimit" and (
                str(value.get("namespace_id", "")) != str(binding["namespace_id"])
                or value.get("simple") != binding["simple"]
            ):
                errors.append(f"{role}.bindings.{name}: rate-limit policy differs or cannot be verified")
            elif kind in ("plain_text", "json") and value.get("text", value.get("json")) != binding.get(
                "text", binding.get("json")
            ):
                errors.append(f"{role}.vars.{name}: differs from inventory")
        if role == "source" and "fulfilment" in self.config:
            for name in ("CARTRAY_ORDER_ACCESS_KEY", "CARTRAY_RESOURCE_CONFIG"):
                if observed.get(name, {}).get("type") != "secret_text":
                    errors.append(f"{role}.bindings.{name}: missing/wrong secret type")
        if self.has_post_purchase and role in POST_PURCHASE_ROLES:
            for name in post_purchase_secret_bindings(role, expected["vars"].get("EMAIL_PROVIDER", "smtp2go")):
                if observed.get(name, {}).get("type") != "secret_text":
                    errors.append(f"{role}.bindings.{name}: missing/wrong secret type")
        return errors

    def doctor(self):
        self.ensure_pages_subdomain()
        self.check_schema()
        self.vault()
        self.pin_suppression()
        nodes, _ = graph(
            self,
            self.state.get("deployment", {}).get("versions"),
            sending=self.state.get("deployment", {}).get("sending", self.state.get("activation_authorized", False)),
        )
        errors = self.private_source_visibility_errors()
        delivery = {}
        if self.has_post_purchase:
            errors.extend(self.post_purchase_visibility_errors("post-purchase"))
            if self.config.get('source_mode','public') == 'public':
                errors.extend(self.post_purchase_visibility_errors('source'))
            try:
                deployment, disabled, build = self.post_purchase_runtime()
                for role in POST_PURCHASE_ROLES:
                    self.artifact_modules(role, build)
                modes = self.state.get("post_purchase_modes", OFF)
                delivery["modes"] = modes
                delivery["fence"] = self.post_purchase_fence()
                if modes not in (OFF, ON) or (modes == OFF and any(
                    deployment["versions"][role] != disabled[role] for role in POST_PURCHASE_ROLES
                )):
                    errors.append("post-purchase: modes differ from the retained deployment")
                if modes == ON and (delivery["fence"]["status"] != "retained"
                                    or "post_purchase_watermark" not in self.state):
                    errors.append("post-purchase: active without a pinned retained fence")
            except (ValueError, OSError) as error:
                errors.append(str(error))
            if self.state.get("post_purchase_transition"):
                delivery["pending"] = self.state["post_purchase_transition"]["operation"]
                errors.append("post-purchase: interrupted transition; resume activation or disable")
        for role, expected in nodes.items():
            actual = self.api.version(self.name(role))
            version = self.state.get("deployment", {}).get("versions", {}).get(role)
            errors.extend(self.worker_binding_errors(role, expected, actual, version))
        if errors:
            return {
                "healthy": False,
                "phase": self.state["phase"],
                "errors": errors,
                "activation_authorized": self.state["activation_authorized"],
                **({"post_purchase": delivery} if self.has_post_purchase else {}),
            }
        record = self.verifier("doctor")
        if self.state["activation_authorized"] and record["status"] != "active":
            errors.append("installation record: " + record["status"])
        return {
            "healthy": not errors,
            "phase": self.state["phase"],
            "activation_authorized": self.state["activation_authorized"],
            "errors": errors,
            "record": record,
            "activation_watermark": self.state.get("activation_watermark"),
            "urls": self.state.get("urls"),
            **({"post_purchase": delivery} if self.has_post_purchase else {}),
        }

    def receipt(self):
        health = self.doctor()
        merchant = self.config.get("store_key", "acorn-store")
        checkpoint = self.api.query(
            self.db("commerce"),
            "SELECT activation_watermark,last_outbox_id FROM post_purchase_checkpoint "
            "WHERE singleton=1 AND merchant_id=? AND environment='test'", [merchant])
        messages = self.api.query(
            self.db("commerce"),
            "SELECT order_id,'order_confirmation' AS purpose,state,provider_reference,"
            "created_at AS attempted_at FROM post_purchase_executions "
            "WHERE merchant_id=? AND environment='test' ORDER BY created_at,order_id", [merchant])
        if self.config['reviews_enabled']:
            messages += self.api.query(
                self.db("commerce"),
                "SELECT order_id,'review_followup' AS purpose,state,provider_reference,attempted_at "
                "FROM order_review_messages WHERE merchant_id=? AND environment='test' "
                "AND due_at IS NOT NULL ORDER BY due_at,order_id", [merchant])
        for message in messages:
            message['order_fingerprint'] = digest(message.pop('order_id').encode())
        if not self.config["reviews_enabled"]:
            result = {
                "format": 2,
                "namespace": self.config["namespace"],
                "doctor": health,
                "resources": self.state["resources"],
                "migration_ledgers": self.state["migration_ledgers"],
                "checkpoint": checkpoint, "order_messages": messages,
                **({"worker_versions": self.state.get("deployment", {}).get("versions"),
                    "post_purchase_config_sha256": self.config["post_purchase_config_sha256"]}
                   if self.has_post_purchase else {}),
            }
            atomic_json(self.directory / "receipt.json", result)
            return result
        choices = self.api.query(
            self.db("commerce"),
            "SELECT b.id AS outbox_id,c.selected,c.merchant_default_selected,g.selection_basis,"
            "CASE WHEN g.order_id IS NULL THEN 0 ELSE 1 END AS granted,"
            "(SELECT COUNT(*) FROM order_items i WHERE i.order_id=b.order_id "
            "AND i.unit_amount_minor>0) AS paid_item_count "
            "FROM outbox b LEFT JOIN review_invitation_checkout_choices c ON c.order_id=b.order_id "
            "LEFT JOIN review_invitation_checkout_grants g ON g.order_id=b.order_id "
            "WHERE b.event_type='OrderConfirmed' ORDER BY b.id",
        )
        deliveries = self.api.query(
            self.db("reviews"),
            "SELECT delivery_identity,provider_email_id,accepted_at FROM review_provider_deliveries "
            "WHERE store_key=? AND environment='test' ORDER BY accepted_at",
            [self.config.get("store_key", "acorn-store")],
        )
        states = self.api.query(
            self.db("reviews"),
            "SELECT state,COUNT(*) AS count FROM invitation_generations "
            "WHERE store_key=? AND environment='test' GROUP BY state",
            [self.config.get("store_key", "acorn-store")],
        )
        public = self.api.query(
            self.db("reviews"),
            "SELECT subject_id,contribution_count,rated_count,version FROM native_publication_versions "
            "WHERE store_key=? AND environment='test' ORDER BY subject_id",
            [self.config.get("store_key", "acorn-store")],
        )
        suppressed = self.api.query(
            self.db("reviews"),
            "SELECT COUNT(*) AS count FROM review_recipient_suppressions "
            "WHERE store_key=? AND environment='test'",
            [self.config.get("store_key", "acorn-store")],
        )
        result = {
            "format": 1,
            "namespace": self.config["namespace"],
            "environment": "test",
            "doctor": health,
            "commit": self.state.get("build"),
            "resources": self.state["resources"],
            "worker_versions": self.state.get("deployment", {}).get("versions"),
            "migration_ledgers": self.state.get("migration_ledgers"),
            "suppression_key_fingerprint": self.state["secret_fingerprints"]["recipient_hash"],
            "checkout_choices": choices,
            "checkpoint": checkpoint,
            "order_messages": messages,
            "provider_acceptances": [m for m in messages if m['state']=='accepted'],
            "legacy_generation_provider_acceptances": deliveries,
            "generation_states": states,
            "public_subjects": public,
            "recipient_suppression_count": suppressed[0]["count"],
        }
        atomic_json(self.directory / "receipt.json", result)
        return result

    def rollback(self, index):
        if self.state.get("post_purchase_provider_migration"):
            raise ValueError("finish credential reconfiguration and OFF deployment before rollback")
        self.require_post_purchase_off()
        self.check_schema()
        self.pin_suppression()
        if not self.state["releases"] or not -len(self.state["releases"]) <= index < len(self.state["releases"]):
            raise ValueError("requested compatible release is not in this installation inventory")
        release = self.state["releases"][index]
        if (
            release["schema"] != self.state.get("migration_ledgers")
            or release["suppression_key_fingerprint"] != self.state["secret_fingerprints"]["recipient_hash"]
        ):
            raise ValueError("rollback schema/key contract is incompatible; databases are never rewound")
        versions = release["versions"]
        release_sending = release.get("sending", bool(release.get("record_revision", 0)))
        reviews_authorized = (self.state.get("activation_authorized")
                              and not self.state.get("reviews_explicit_activation_required"))
        if self.config["reviews_enabled"] and bool(release_sending) != bool(reviews_authorized):
            raise ValueError("rollback release is incompatible with the current Reviews activation state")
        if self.has_post_purchase:
            self.vault()
            self.verify_merchant_inputs()
            self.check_post_purchase_release(release)
            if release.get("post_purchase_modes") != OFF or set(versions) != set(self.roles):
                raise ValueError("post-purchase rollback requires a recorded compatible OFF release")
            for role in POST_PURCHASE_ROLES:
                self.artifact_modules(role, release)
                actual = self.api.request(f"workers/scripts/{self.name(role)}/versions/{versions[role]}")
                self.check_post_purchase_version(role, "disabled", actual, versions[role])
            self.check_current_post_purchase_off(
                versions if self.state["phase"] == "rolling-back" else None)
        self.state["phase"] = "rolling-back"
        self.save()
        publish_roles = versions
        for role in publish_roles:
            version = versions[role]
            private = role == "post-purchase" or (
                role == "source" and self.config.get("source_mode", "public") == "private")
            if private:
                self.set_worker_visibility(role, False)
            self.api.publish(self.name(role), version)
            if private:
                self.set_worker_visibility(role, False)
        visibility_errors = self.private_source_visibility_errors()
        if self.has_post_purchase:
            visibility_errors.extend(self.worker_visibility_errors("post-purchase"))
            for role in POST_PURCHASE_ROLES:
                self.check_post_purchase_version(role, "disabled", self.api.version(self.name(role)), versions[role])
        if visibility_errors:
            raise ValueError("; ".join(visibility_errors))
        self.state["build"] = release["commit"]
        self.state["deployment"] = {
            "commit": release["commit"],
            "versions": dict(versions),
            "complete": True,
            "sending": release_sending if self.config["reviews_enabled"] else not self.has_post_purchase,
        }
        if self.has_post_purchase:
            self.state["deployment"]["post_purchase_disabled_versions"] = {
                role: versions[role] for role in POST_PURCHASE_ROLES
            }
        self.save()
        reviews_ready = self.config["reviews_enabled"] and self.state.get("activation_authorized", False)
        record = self.verifier("record") if reviews_ready or not self.config["reviews_enabled"] else {"revision": 0}
        paused = (self.state.get("automation_paused", False)
                  or self.state.get("reviews_explicit_activation_required", False)
                  or (self.config["reviews_enabled"] and not reviews_ready))
        self.state["queue_recovery_required"] = paused
        self.state["phase"] = "paused" if paused else "active"
        if (self.has_post_purchase and not self.config["reviews_enabled"]) or (
            self.config["reviews_enabled"] and not reviews_ready
        ):
            self.state["phase"] = "deployed"
        self.save()
        return {
            "phase": self.state["phase"],
            "rollback_commit": release["commit"],
            "record_revision": record["revision"],
            "database_rewind": False,
        }

    def db(self, role):
        return self.state["resources"]["database:commerce"]["id"]

    def migrations(self):
        chains = {
            "commerce": sorted((ROOT / "migrations").glob("*.sql")),
            "subscriber": sorted((ROOT / "modules/reviews/automation-intake-producer/migrations").glob("*.sql")),
            "reviews": [
                ROOT / "modules/reviews" / item
                for item in json.loads((ROOT / "modules/reviews/module.json").read_text())["migrations"]
            ],
            "context": sorted((ROOT / "modules/reviews/consent/migrations").glob("*.sql")),
        }
        return chains if self.config["reviews_enabled"] else {"commerce": chains["commerce"]}

    def migrate(self):
        self.require_post_purchase_off()
        self.assert_resources()
        self.vault()
        for role, paths in self.migrations().items():
            db = self.db(role)
            self.api.query(
                db,
                "CREATE TABLE IF NOT EXISTS cartray_operator_migrations "
                "(namespace TEXT NOT NULL, position INTEGER NOT NULL, path TEXT UNIQUE NOT NULL, "
                "checksum TEXT NOT NULL, PRIMARY KEY(namespace,position))",
            )
            namespaces = self.api.query(db, "SELECT DISTINCT namespace FROM cartray_operator_migrations")
            if {row["namespace"] for row in namespaces} - set(self.migrations()):
                raise ValueError("unknown migration namespace")
            rows = self.api.query(
                db,
                "SELECT position,path,checksum FROM cartray_operator_migrations WHERE namespace=? ORDER BY position",
                [role],
            )
            expected = [
                {"position": i + 1, "path": str(path.relative_to(ROOT)), "checksum": digest(path.read_bytes())}
                for i, path in enumerate(paths)
            ]
            if rows != expected[: len(rows)]:
                raise ValueError(
                    f"{role}: unknown, reordered or changed migration; no downgrade or repair is automatic"
                )
            for item, path in zip(expected[len(rows) :], paths[len(rows) :], strict=True):
                sql = (
                    path.read_text()
                    + "\nINSERT INTO cartray_operator_migrations VALUES "
                    + (f"('{role}',{item['position']},'{item['path']}','{item['checksum']}');\n")
                )
                self.execute_file(db, sql)
            self.state.setdefault("migration_ledgers", {})[role] = expected
            self.save()
        if "fulfilment" in self.config:
            db = self.db("commerce")
            self.api.query(db, "INSERT OR IGNORE INTO post_purchase_installation VALUES (1,?,?)",
                           [self.config["store_key"], "test"])
            scope = self.api.query(
                db, "SELECT merchant_id,environment FROM post_purchase_installation WHERE singleton=1"
            )
            if scope != [{"merchant_id": self.config["store_key"], "environment": "test"}]:
                raise ValueError("fulfilment installation scope differs; restore the original installation")
            vault = self.vault()
            if "order_access" not in self.state["secret_fingerprints"]:
                count = self.api.query(db, "SELECT count(*) AS n FROM order_access")
                if len(count) != 1 or count[0]["n"]:
                    raise ValueError("order access key missing with existing capabilities; restore the original key")
                if "order_access" not in vault:
                    vault["order_access"] = secrets.token_hex(32)
                    atomic_json(self.vault_path, vault)
                self.state["secret_fingerprints"]["order_access"] = digest(vault["order_access"].encode())
                self.save()
        self.pin_suppression(allow_create=True)
        self.state["phase"] = "migrated"
        self.save()
        return {role: len(paths) for role, paths in self.migrations().items()}

    def execute_file(self, database, sql):
        config = self.directory / "d1.toml"
        config.write_text(
            f'account_id = "{self.config["account_id"]}"\n'
            f'[[d1_databases]]\nbinding = "DB"\ndatabase_name = "{database}"\n'
            f'database_id = "{database}"\n'
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", dir=self.directory) as stream:
            stream.write(sql)
            stream.flush()
            result = subprocess.run(
                [
                    str(ROOT / "node_modules/.bin/wrangler"),
                    "d1",
                    "execute",
                    "DB",
                    "--remote",
                    "--yes",
                    "--config",
                    str(config),
                    "--file",
                    stream.name,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                env={**os.environ, "CLOUDFLARE_API_TOKEN": self.credentials["cloudflare_api_token"]},
            )

            if result.returncode:
                log = self.directory / "migration-error.log"
                fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(result.stdout + result.stderr)
                raise CloudError(
                    f"D1 migration failed; inspect protected {log}, then retry migrate; no ledger repair is automatic"
                )

    def pin_suppression(self, allow_create=False):
        if not self.config["reviews_enabled"]:
            return
        self.assert_resources()
        vault = self.vault()
        params = [self.config.get("store_key", "acorn-store"), "test", self.config["namespace"]]
        check = hmac.new(
            vault["recipient_hash"].encode(),
            json.dumps(["reviews-suppression-key-check-v1", *params, "key1"], separators=(",", ":")).encode(),
            "sha256",
        ).hexdigest()
        current = self.api.query(
            self.db("context"),
            "SELECT c.key_version,r.key_check FROM "
            "review_suppression_key_current c JOIN review_suppression_key_registry r "
            "USING(store_key,environment,installation_id,key_version) "
            "WHERE c.store_key=? AND c.environment=? AND c.installation_id=?",
            params,
        )
        if current:
            if current != [{"key_version": "key1", "key_check": check}]:
                raise ValueError("suppression key identity mismatch; restore original key, no rotation is performed")
            if not self.state.get("suppression_pinned"):
                self.state["suppression_pinned"] = True
                self.save()
            return
        if not allow_create or self.state.get("suppression_pinned"):
            raise ValueError(
                "suppression registry is missing; restore original registry; no identity repair is automatic"
            )
        registered = self.api.query(
            self.db("context"),
            "SELECT key_check FROM review_suppression_key_registry WHERE store_key=? AND environment=? "
            "AND installation_id=? AND key_version='key1'",
            params,
        )
        if registered and registered != [{"key_check": check}]:
            raise ValueError("suppression registry differs; restore the original key")
        self.api.query(
            self.db("context"),
            "INSERT INTO review_suppression_key_registry VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            [*params, "key1", check],
        )
        self.api.query(
            self.db("context"), "INSERT INTO review_suppression_key_current VALUES (?,?,?,?)", [*params, "key1"]
        )
        self.state["suppression_pinned"] = True
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="private installation state directory")
    parser.add_argument("--credentials", type=Path, help="private documented test credential handoff JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="write a non-secret plan; creates no cloud resources or keys")
    plan.add_argument("--config", type=Path, required=True)
    for name in ("provision", "migrate", "build", "deploy", "verify", "doctor", "upgrade", "receipt", "pause"):
        command = commands.add_parser(name)
        if name == 'build':
            command.add_argument('--merchant-inputs', type=Path,
                                 help='explicitly snapshot corrected merchant inputs while sending is OFF')
    activation = commands.add_parser(
        "activate", help="explicitly authorize the fixed no-backfill fence and test delivery"
    )
    activation.add_argument("--enable-test-delivery", action="store_true")
    rollback = commands.add_parser("rollback", help="compatible recorded Worker versions only; never rewind D1")
    rollback.add_argument("--release", type=int, default=-2, help="inventory release index (default previous)")
    request = commands.add_parser("request", help="operator-side call to the existing private Reviews service")
    request.add_argument("--path", required=True)
    request.add_argument("--method", choices=["GET", "POST"], default="GET")
    request.add_argument("--body", type=Path)
    request.add_argument("--admin", action="store_true")
    args = parser.parse_args()
    try:
        args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (args.directory / ".operator.lock").open("w") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "plan":
                # The lock is not installation content.
                if (args.directory / "inventory.json").exists():
                    raise ValueError("inventory already exists; resume its commands instead")
                config = json.loads(args.config.read_text())
                installation = Installation.plan(args.directory, config)
                result = {
                    "phase": "planned",
                    "workers": {role: installation.name(role) for role in installation.roles},
                    "databases": {role: installation.name(role) for role in DATABASES},
                    "queues": {role: installation.name(role) for role in installation.queues},
                    "pages": None if "storefront_origin" in installation.config else installation.name("store"),
                }
            else:
                credentials = read_private(args.credentials) if args.credentials else None
                installation = Installation(args.directory, credentials)
                if args.command == "activate":
                    result = installation.activate(args.enable_test_delivery)
                elif args.command == "rollback":
                    result = installation.rollback(args.release)
                elif args.command == "upgrade":
                    result = installation.upgrade()
                elif args.command == 'build':
                    if args.merchant_inputs:
                        installation.update_merchant_inputs(args.merchant_inputs)
                    result = installation.build()
                elif args.command == "request":
                    result = installation.private(
                        "REVIEWS",
                        args.path,
                        json.loads(args.body.read_text()) if args.body else None,
                        installation.vault()["admin"] if args.admin else None,
                        args.method,
                    )
                else:
                    result = getattr(installation, args.command)()
            print(json.dumps(result, indent=2))
    except (ValueError, CloudError, OSError, subprocess.CalledProcessError) as error:
        message = (
            str(error)
            if not isinstance(error, subprocess.CalledProcessError)
            else ("required local build/migration command failed; retained installation state is resumable")
        )
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
