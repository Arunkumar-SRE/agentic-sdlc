Review the result for risk before it goes to a human.

Requirement analysis:
{{ outputs.requirements }}

Design:
{{ outputs.design }}

Verification results:
{{ outputs.verify }}

Run history (retries, fallbacks, repair rounds):
{{ run.events }}

List risks (likelihood, impact, mitigation, status), failure scenarios with detection and behaviour, the validation
strategy that was actually applied, and remaining limitations. Mark a risk verified_by_test only if a passing test
covers it.

Feedback from previous attempts:
{{ feedback }}
