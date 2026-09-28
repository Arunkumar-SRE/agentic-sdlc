Reason about the existing codebase to scope a brownfield change.

Requirement analysis:
{{ outputs.requirements }}

Static analysis of the repository (AST inventory, request flows, ranked impact):
{{ tools.analyze_codebase }}

Identify the impacted modules (path, role, why, symbols), the request flows that change, the data flow notes a
developer must respect (ordering, transactions, error isolation), the existing tests to update, and change risks.

Feedback from previous attempts:
{{ feedback }}
