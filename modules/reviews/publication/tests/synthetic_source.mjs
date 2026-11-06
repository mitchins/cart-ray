// Test-only HTTP source, generated from an actual local synthetic R4 response.
// This is a fixture adapter, not an independently qualified Reviews Worker.
import fixture from './synthetic_source_data.mjs';
const headers = { 'Cache-Control': 'no-store', 'Content-Type': 'application/json' };
export default {
  async fetch(request) {
    if (request.method !== 'GET') return new Response(null, { status: 405 });
    const url = new URL(request.url);
    if (url.pathname === '/reviews/v1/subject-lookup' && url.searchParams.get('alias') === fixture.subject.id) {
      return Response.json({ subject_id: fixture.subject.id, kind: fixture.subject.kind, canonical_path: fixture.subject.canonical_path }, { headers });
    }
    if (url.pathname === `/reviews/v1/subjects/${fixture.subject.id}/summary`) return Response.json(fixture.summary, { headers });
    if (url.pathname === `/reviews/v1/subjects/${fixture.subject.id}/contributions` && !url.searchParams.has('after')) {
      const page = { ...fixture.pages[0] }; delete page.request_after;
      return Response.json(page, { headers });
    }
    return new Response(null, { status: 404, headers });
  }
};
