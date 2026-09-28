# Test plan

## contract
- Every operation in the agreed OpenAPI is served
- Create accepts every contract field
## regression
- The pre-existing suite is copied with the repository and must still pass
## new behaviour
- N redirects then 410
- remaining_clicks in analytics
- unset = unlimited
- max_clicks >= 1

## Acceptance criteria covered
- GET /{code} returns 302 to the long URL; unknown code returns 404
- GET /api/v1/urls/{code}/analytics reports total clicks, last access, clicks by day and top referrers
- A link with max_clicks=N redirects N times, then returns 410; analytics shows remaining clicks
- All pre-existing tests still pass (no regression) and existing API fields are unchanged
