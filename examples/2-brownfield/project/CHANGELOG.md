# Changelog

## Unreleased

Brownfield feature: Redirect short codes to long URLs with correct 404/410 semantics; Record and expose click analytics; Enforce a per-link click limit.

- app/ops.py (route layer): identifiers match click, redirect (score 12.21); symbols: chaos_before_redirect
- app/services.py (business logic): identifiers match analytic, click, redirect (score 11.54); symbols: get_analytics, record_click
- app/main.py (route layer): identifiers match analytic, redirect (score 8.64); symbols: analytics, redirect
- app/schemas.py (schema / DTO): identifiers match analytic, click (score 6.49); symbols: AnalyticsResponse, ClickEvent
- app/models.py (data model): identifiers match click (score 4.43); symbols: Click
- models.URL: add max_clicks INTEGER NULL and click_count INTEGER NOT NULL DEFAULT 0
- schemas: URLCreate.max_clicks (optional, >= 1); URLResponse.max_clicks; AnalyticsResponse.max_clicks/remaining_clicks
- services.resolve_url: consume one click with a conditional UPDATE (click_count < max_clicks); 0 rows -> 410 LinkExhausted
- services.get_analytics: report max_clicks and remaining_clicks
- Backward compatibility: new request fields optional, no existing field changed; verified by the old suite
