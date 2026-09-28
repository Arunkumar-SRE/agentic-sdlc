# Test plan

## contract
- Every operation in the agreed OpenAPI is served
- Create accepts every contract field
## unit/service
- Alias and expiry validation incl. timezone-aware input
- Collision retry and exhaustion (503)
- Click-write failure does not break the redirect
- Rate limit returns 429
## integration
- Create -> redirect -> analytics through the ASGI app with an isolated in-memory DB
- 404 / 409 / 410 / 422 semantics
- Health, readiness, metrics, request id

## Acceptance criteria covered
- POST /api/v1/urls returns 201 with a short_code; malformed or non-http(s) URLs return 422
- GET /{code} returns 302 to the long URL; unknown code returns 404
- Links and clicks are stored in a relational database with a UNIQUE short_code
- GET /api/v1/urls/{code}/analytics reports total clicks, last access, clicks by day and top referrers
- API is stateless; lookups use an indexed unique key; the path to Postgres, caching and async analytics is documented
