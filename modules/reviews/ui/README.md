# Reviews invitation form

These static files are a reusable, test-only customer form. Serve them from the same origin as a Reviews Worker configured in `verified_acquisition_only` test mode, with `/reviews/v1/` proxied to that Worker. The link has the form `/review/#invitation=<capability>`; the fragment is not sent in the initial page request. Configure a public store name and set an approved display name for each subject before opening an invitation.

The form keeps its draft and retry identifier in tab-scoped session storage. It does not store the invitation token there. A bearer link can be forwarded, so it verifies acquisition eligibility, not the identity of the person who opens it.

This is not a production release. The local flow supports replacement requests and review management. A test-only queue consumer can dispatch recovery when explicitly configured; real recipient integration, real staff identity, live sending and production qualification remain open.
