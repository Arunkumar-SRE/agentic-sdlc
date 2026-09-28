Write the API contract and database schema that implementation and tests will both be built from.

Design:
{{ outputs.design }}

Requirement analysis:
{{ outputs.requirements }}

Return an OpenAPI 3.1 document (paths, request/response schemas under components.schemas, error responses) and DDL
for "sqlite" and "postgresql". For brownfield work, changes must be additive and backward compatible; list migration
and compatibility notes.

Feedback from previous attempts:
{{ feedback }}
