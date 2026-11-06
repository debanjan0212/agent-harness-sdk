# Incident Commander

An AI incident manager built on `agent-harness-sdk`. It shows what "a thin wrapper on the base SDK" looks like
when the result is a product.

**Alert in → investigate → severity + root cause → one safe action (with approval) → timeline + postmortem draft.**

| Piece | File | What it adds on top of the SDK |
|---|---|---|
| Domain schema | `domain.py` | `IncidentAnalysis`: severity SEV1–SEV4, impacted services, customer impact, suspect change; the playbook prompt |
| Domain tools | `tools.py` | read: `service_health`, `service_dependencies`, `recent_deploys`, `search_logs`, `similar_incidents` · act: `rollback_deploy`, `scale_service` · escalate: `page_oncall`, `notify` |
| Data sources | `sources.py` | `FixtureSource` (offline scenarios) or `LiveSource` (Prometheus, Loki, Harness pipelines) |
| Store | `store.py` | SQLite incidents + timeline |
| Wrapper | `commander.py` | domain policy rule *no rollback without a recent deploy*, product metrics, `incident.handle` span, timeline hook, postmortem |
| API | `server.py` | Alertmanager webhook, incidents, approvals (approve / deny), postmortem |
| Offline brain | `brain.py` | deterministic playbook model so everything runs with no API key |

## Run

```
cd examples
python -m incident_commander demo --scenario bad-deploy --approve ask
python -m incident_commander serve --port 8080
```

```
curl -X POST localhost:8080/webhooks/alertmanager -H 'content-type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"HighErrorRate","service":"frontend","severity":"critical"},"annotations":{"summary":"5xx above 5%"}}]}'
curl localhost:8080/approvals
curl -X POST localhost:8080/incidents/<id>/approve -H 'content-type: application/json' -d '{"by":"you"}'
curl localhost:8080/incidents/<id>/postmortem
```

| Env | Meaning |
|---|---|
| `IC_SOURCE=live` | Investigate the real stack (`PROMETHEUS_URL`, `LOKI_URL`, `HARNESS_*`) instead of a fixture |
| `IC_SCENARIO` | Which fixture to replay when not live: `bad-deploy`, `db-pool`, `injection` |
| `IC_DB` | SQLite path (default in-memory) |
| `DEEPSEEK_API_KEY` | Use DeepSeek instead of the offline playbook brain |

Deploy: `docker compose -f deploy/docker-compose.yaml up --build` locally, or the Helm chart in `deploy/helm/`.
