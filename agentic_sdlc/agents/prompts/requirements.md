Analyse this software requirement and normalise it into a clear engineering problem.

Requirement:
{{ requirement }}

Run inputs (mode may force greenfield/brownfield): {{ inputs }}

Produce:
- change_type: greenfield (new system) or brownfield (change to existing code: enhancements, fixes, refactors, tests, docs).
- intent: feature | bugfix | refactor | tests | docs.
- goals, and testable acceptance_criteria (each one checkable by an automated test or a command).
- ambiguities: for every unclear point, a question for the stakeholder AND the working assumption you will use.
- clarity = "ambiguous" and a low confidence when the requirement cannot be implemented without those assumptions.
- confidence in [0, 1]: how safely the work can proceed without a human answering the questions.
- keywords: domain identifiers useful for searching the codebase.

Feedback from previous attempts:
{{ feedback }}
