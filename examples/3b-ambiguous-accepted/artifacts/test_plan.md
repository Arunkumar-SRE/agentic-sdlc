# Test plan

## contract
- Every operation in the agreed OpenAPI is served
- Create accepts every contract field
## regression
- The pre-existing suite is copied with the repository and must still pass

## Acceptance criteria covered
- GET /api/v1/urls/{code}/analytics reports total clicks, last access, clicks by day and top referrers
- A link past expires_at returns 410
- All pre-existing tests still pass (no regression) and existing API fields are unchanged
