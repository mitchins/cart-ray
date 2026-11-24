# Installed catalogue refresh qualification

Scope: an existing OFF test merchant, expanded catalogue inputs and private
resource configuration. No production activation, hosted EP installation change,
mail send, order/checkpoint reset or new deployment topology.

## Actual package replay

The EP consumer supplied its corrected public/dummy merchant package:
`3d134152af1352e509a674f7eb1a7c1b571c50b90506da2b2dee8e11c2a2ce7f`.
Its compiled catalogue is
`sha256:2d6934024c50a63787212bfbf7e5572c5e7358e9518623d417aa9dc28d41f4ed`.

A disposable local copy of the actual two-offer installation inventory and inputs
passed the real compiler, snapshot replacement and full merchant validation:
94 offer keys, 92 canonical review subjects. The three consulting variants share
the exact approved `/product/ndis-consulting/` page and native subject, with
separate immutable purchase actions. Repeating the operation left inventory bytes
unchanged. Resource IDs, key fingerprints and activation history were retained;
the original inventory was unchanged. No vault or cloud credential was loaded.
Read-only provider verification was stubbed for this **local replay**, not claimed
as hosted verification. The EP operator must perform real OFF/deployment checks.

The real compiler first caught an upstream source CSV containing the derived
`asset_hashes_json` column. The consumer corrected its generator. Kernel CSV
validation was kept strict; actual asset fingerprints are recomputed at build.

## Resource transport and runtime

The EP operator's disposable private Cloudflare probe rejected a single 36,367-byte
secret (HTTP 400, provider code 10054); the same API accepted a 100-byte control.
Using CartRay's pure packer, the 36,367-byte dummy payload became twelve parts plus
metadata, each binding at most 4096 bytes. The native upload API accepted it and
an actual private service-binding invocation returned the exact byte count/hash:
`c526bafe6cbed2bd5671097d1de9c1b339924a5b48b439f7692f1a497369c11a`.
The disposable probe was cleaned up. Its diagnostic JavaScript decoder qualifies
transport, not the native Python resource endpoint or paid delivery.

The separate local Python Workerd/D1 smoke used a large packed configuration
through the actual order-access route: purchased private file and external access
passed, with the exact private file response. Kinglet/SQLite route tests also
cover denial of unpurchased items. Codec tests cover Unicode, the 65536-byte total
limit, bounded parts and corrupt/missing/extra/mixed bindings. Operator tests cover
single-secret to multipart and subsequent part-count changes while OFF, using
recorded installed shapes and exact incoming candidate shapes. Keys and SQLite
state remain unchanged.

Actual hosted EP purchased-resource and full customer qualification remains owned
by the EP consumer after installing the reviewed release. These component tests
do not authorise production or substitute for that hosted acceptance.
