# Contributing

## The one rule

**Change the public API, update both guides in the same commit.**

| Audience | File |
|---|---|
| People | `README.md` (renders on the GitHub project page) |
| AI assistants | `llm/spotai-agent-guide.md` |

This is not a convention you have to remember — `tests/test_docs_current.py`
fails the build when a public method, exported name, or model field is
undocumented. If it fails, document the thing rather than adding it to the
exemption list.

## Setup

```bash
pip install -e ".[dev]"
pytest -q
```

Tests need no API key and make no network calls.

## Adding an endpoint

1. Add a method to `SpotAI` in `client.py` that delegates to
   `self.http.request(...)`. Keep it thin.
2. Add it to the method reference table in `README.md`.
3. Add its signature to section 6 of `llm/spotai-agent-guide.md`.
4. If it has a limit or a surprising behaviour, add a row to the "surprises"
   table in both guides.

## Adding a workflow

Anything multi-step goes in its own module beside `damage_claims.py`, exposed
as a thin method on `SpotAI`. Keep pure logic (naming, identity, time maths,
status) in `claims.py` or `timewin.py` — that is the part that gets unit
tested without a network.

## Writing style

- **README**: for beginners and people who code occasionally. Plain words,
  worked examples, say *why*. Avoid jargon; when a term is unavoidable,
  explain it once.
- **LLM guide**: dense and technical. Exact signatures, exact limits, explicit
  anti-patterns. Lead with the rules that cause silent breakage.

## Tests

Cover the parts where mistakes are silent rather than loud: timezone
conversion, offset seeding, name truncation, identity construction, status
derivation. A wrong timezone produces a plausible-looking clip of the wrong
moment, which is worse than a crash.

Name tests for the behaviour, not the function:
`test_date_is_the_sites_date_not_the_servers`.

## Releasing

1. Bump `__version__` in `spotai/__init__.py` and `version` in
   `pyproject.toml`
2. Update the version line at the top of `llm/spotai-agent-guide.md`
   (a test enforces this)
3. `pytest -q`
4. Tag `vX.Y.Z` and push — the install line pins by tag
