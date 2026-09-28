Implement the design so that it serves exactly the agreed contract.

Requirement analysis:
{{ outputs.requirements }}

Design:
{{ outputs.design }}

Contract:
{{ outputs.contract }}

Codebase impact (brownfield only; absent otherwise):
{{ outputs }}

Greenfield: return complete files for a FastAPI + SQLAlchemy 2 service in an `app/` package with `app.main:app`, plus
requirements.txt. Brownfield: return `edits` (exact find/replace; each `find` must occur exactly once in the current
file) against the existing files; do not rewrite whole files. Production quality: validation at the boundary, domain
errors mapped to HTTP, no check-then-insert races, structured logging.

Verification feedback from previous rounds (fix every item):
{{ feedback }}
