Decompose the work into a dependency-ordered task plan.

Requirement analysis:
{{ outputs.requirements }}

Codebase impact (absent for greenfield work):
{{ outputs }}

Each task: id (letter + number), title, owner_step (one of: impact, design, contract, codegen, testgen, docs, verify,
risks, report), depends_on (task ids), deliverable, acceptance. The plan must be a DAG. Prefer parallel branches
where work is independent (e.g. tests written from the contract in parallel with the implementation). Explain the
sequencing in `rationale`.

Feedback from previous attempts:
{{ feedback }}
