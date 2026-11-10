# GitHub-source handoff for Codex

This directory is a **complete source-only snapshot** of the CartRay GEL and Minimal, Studio, Classic theme deliverable. It contains no checkout or server changes and must not activate any production feature. It exists here because ChatGPT ZIP downloads were failing.

Build four previews:

```sh
cd examples/gel-theme-pack
python3 build.py --all --output dist
python3 serve.py --directory dist --port 4173
```

Run tests:

```sh
npm test
# optional, requires Playwright + Chromium and HTTP preview server:
python3 tests/browser_test.py
```

Start repository integration with `CODEX_HANDOFF.md`. The independent presenter imports CartRay's actual cart, checkout, return status, and review clients; sample preview payment and reviews are simulations. Do **not** merge the demo adapter into production.

**Source-only choice:** `dist/` (four rebuildable demos) and `screenshots/` (~36 rebuildable captures) are not committed; `THEMES-OVERVIEW.webp` and `WHITELABEL-OVERVIEW.webp` provide a quick design review. This avoids bloating the permanent Git history with generated assets. The original full ZIP remains preserved in the conversation, but its download route fails.

The originally collected browser evidence in `evidence/` was produced against the earlier full ZIP and is historical; running the browser harness from this PR is required for fresh results.

Do not block OPERABILITY-01 PR B or change Effective Policy/WooCommerce migration, Stripe authority, Reviews consent/suppression, runtime attestation or delivery semantics.
