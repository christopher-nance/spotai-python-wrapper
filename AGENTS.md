# Agent instructions

Working **on** this repository? Read `CONTRIBUTING.md`.

Working **with** this library in another project? Use
[`llm/spotai-agent-guide.md`](llm/spotai-agent-guide.md) — download it and drop
it into your project as `CLAUDE.md` or `AGENTS.md`.

## Non-negotiables when changing this repo

1. Public API changes must update `README.md` **and**
   `llm/spotai-agent-guide.md` in the same commit.
   `tests/test_docs_current.py` enforces this.
2. Never commit `.env`, real API keys, licence plates, camera IDs tied to a
   named site, or customer footage.
3. `transport.py` is HTTP only. `client.py` stays thin. Workflows get their own
   module.
4. Do not "fix" the base URL — `dev-api.spot.ai` is correct and serves
   production.
5. `pytest -q` must pass. Tests never make network calls.
