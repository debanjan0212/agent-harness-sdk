# Security

## Reporting a problem

Email **itsme.deb1995@gmail.com** with what you found and how to reproduce it.
Please do not open a public issue for anything exploitable.

## What the harness enforces

These rules run in code, never in the prompt, so they hold when the model is
wrong or manipulated ([`agent_harness_sdk/guardrails.py`](agent_harness_sdk/guardrails.py),
[`agent_harness_sdk/approvals.py`](agent_harness_sdk/approvals.py)):

- **Read first.** Agents gather evidence with read-only tools.
- **One action, checked by policy.** A decided action passes policy written in code before anything runs.
- **A person approves changes.** Approval-required actions wait for an Approver. A denial is escalated to a person.
- **Secrets are redacted** from every tool result before the model sees it: AWS keys, JWTs, bearer tokens, key=value secrets, private keys, Harness tokens, passwords inside connection URLs, and card numbers (13 to 16 digits that pass the card checksum, so timestamps and order ids are left alone).
- **Prompt injection is flagged.** Instruction-like text in tool output is treated as data and recorded.
- **Budgets.** Hard limits on steps, tool calls, tokens, output size and run time.
- **Everything is recorded** as OpenTelemetry traces, logs, metrics and events.

The test suite and the offline demos cover a bad release, an incident with no
safe fix, and a prompt injection hidden in the logs.

## What is checked on every change

[`.github/workflows/security.yml`](.github/workflows/security.yml) runs on every
push to `main`, every pull request and once a week:

| Check | Tool | Fails the run when |
|---|---|---|
| Dependencies, with every integration in `all` installed | pip-audit | any known vulnerability |
| Code, SDK and examples | Bandit | any medium or high finding |
| Secrets, full Git history | gitleaks | anything not listed in `.gitleaks.toml` |
| Dependencies, secrets, Helm chart and Dockerfile config | Trivy | anything high or critical with a fix |
| Container image | Trivy | a critical issue with a fix; everything else is recorded in the Security tab |
| Static analysis | CodeQL, `security-extended` | results appear in the Security tab |

Suppressions are rare and each one says why: two `# nosec` comments in the
Incident Commander example and one entry in `.gitleaks.toml` for made-up test
credentials.

## What this does not claim

This is an open-source SDK, not a certified product. SOC 2 and HIPAA are
audits of an organisation and how it operates. The rules above are the kind of
controls those audits ask about (change approval, least privilege, audit
logging, data minimisation), and the telemetry makes them easier to evidence.
