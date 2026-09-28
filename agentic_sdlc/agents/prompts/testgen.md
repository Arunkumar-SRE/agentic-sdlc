Write the automated tests from the contract and acceptance criteria, independently of any implementation.

Requirement analysis:
{{ outputs.requirements }}

Contract:
{{ outputs.contract }}

Return pytest files under tests/ (test_*.py, conftest.py) covering unit, integration and contract layers, plus a
`strategy` map (layer -> what it covers) and the acceptance criteria covered. Use FastAPI's TestClient with an
isolated in-memory database. For brownfield work, the existing tests remain; add tests for the new behaviour only.

Feedback from previous attempts:
{{ feedback }}
