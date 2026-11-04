# BankQL security

Status as of 2026-11-04 (commit after `edcdcde`). Enforcement is **ours**: the sqlglot validator, column-lineage
PII rules, the small-count rule, connection-level read-only access, and our own output check. SQLAS is
**advisory only** (warnings, evaluation, failure classification).

## Results

`tests/security/` — **50 passed, 0 failed** (run on SQLite, DuckDB and PostgreSQL 16 where parametrised).
Core guard suites (`test_validator`, `test_pii_lineage`, `test_guardrails`, `test_connectors`) — **259 passed**.
Full suite — **527 passed, 1 skipped** (the skip is the DuckDB-extension federation blocker, unrelated to security).

| # | Control | Tests | Result |
|---|---|---|---|
| D1 | No PII value from the DB in any prompt: PII columns are never sampled (so absent from M-Schema, the entity index and the Redis schema cache); session history stores ids, not names, for clarifications; client-supplied history is reduced to first lines and PII-masked | `test_pii_never_in_prompts.py` (6) | PASS |
| D2 | Small-count rule: PII only counted or AND-filtered; outputs only COUNT + non-PII group keys; counts < 5 → `"<5"` (also in exports) | `test_small_counts.py` (23), `test_pii_lineage.py` | PASS — letter-by-letter probe of client 12 returns `"<5"` for all 26 letters; OR / NOT / CASE / row-level / MIN / UNION probe shapes refused |
| D2 | Per-key rate limits (asks, lookups per minute), 429 + `Retry-After`, per key, audited | `test_small_counts.py::test_lookup_rate_limit_per_key` | PASS |
| D3 | llama-server bound to 127.0.0.1 with `--api-key`; our client and Mercer's backend send it | `test_side_services.py` (4) | PASS |
| D3 | Redis 127.0.0.1 + `--requirepass` (demo script, compose); compose publishes ports on 127.0.0.1 only | `test_side_services.py` | PASS (static checks of the scripts) |
| D3 | Exports: 128-bit random ids, bound to the creating key (other keys get 404), 24 h expiry + purge, directory 0700 | `test_access.py` | PASS |
| D3 | MCP `run_sql` off by default (tool not registered); REST `/v1/sql/run` 403 unless the key has `run_sql: true` | `test_mcp.py`, `test_api.py` | PASS |
| D3 | Per-key source allowlists: 403 on ask / tables / validate / run; `/v1/sources` and `/v1/models` filtered; unknown source 404 without listing others | `test_access.py` | PASS |
| D3 | Errors: no tracebacks, driver text, SQL echoes, connection strings; generic 500 handler; details only in the audit log | `test_access.py` | PASS |
| D3 | API keys stored as SHA-256 only; constant-time comparison; fail closed with no keys | `test_access.py`, `test_api.py` | PASS |
| D4 | Audit log: names, contacts and PII literals in SQL masked (`[PII]`, `'***'`); file 0600; retention purge (default 90 days) | `test_audit_privacy.py` (4) | PASS |
| D5 | Indirect prompt injection in DB values: dropped from M-Schema samples; replaced by `[filtered]` in summary prompts and template summaries; summaries containing instructions/URLs/contacts replaced; routing and SQL unchanged; values still returned verbatim as data | `test_indirect_injection.py` (3) | PASS |

Found and fixed while building the suite: the template fallback summary echoed an injected cell verbatim; a
corrector "fix" to `SELECT 1` (reads no table) was accepted as an answer; `bankql check-source` read the session's
`default_transaction_read_only` (set by BankQL itself) instead of the role-level setting.

## Residual risks (not closed)

1. **Differencing attacks on counts.** Two allowed counts ≥ 5 that differ by one person reveal a fact about that
   person (`COUNT … WHERE id IN (S) AND name LIKE 'A%'` vs. the same with 12 added). Small-count suppression cannot
   stop this; only noise (differential privacy) or query auditing can. Mitigated by per-key rate limits and the
   audit log. **Recommendation:** add per-key query budgets on PII-filtered queries, or DP noise for those counts.
2. **SQLAS is advisory.** A prompt-injection *question* reaches the router and Mercer. Everything they produce is
   still validated (read-only, allowlist, PII lineage, small counts), so the worst case is a wrong-but-safe answer
   or a refusal — not a write or a PII leak.
3. **Injection detection is a pattern list** (`guardrails.looks_like_injection`). Novel phrasing in a DB value
   can reach Mercer samples or the summary prompt; summary grounding (numbers must be in the data) and the
   output check limit what it can do. Treat summaries as untrusted text in the calling app.
4. **Rate limiter and sessions are per process.** Several API workers multiply the limits. Use one worker or move
   both to Redis before scaling out.
5. **Name masking** uses the `client_profile` names (one regex, fine to ~10⁵ names) plus contact regexes; other
   free-text PII in questions is not detected.
6. **Driver-level read-only** is inspected only for PostgreSQL, SQLite and DuckDB. MySQL / SQL Server / Oracle
   rely on SELECT-only grants (docs/CONNECT.md part 2); `check-source` prints a WARN for them.
7. **Per-user permissions** are not enforced yet: `user_id` is accepted and audited only (PROGRESS.md "Later").
8. **MCP over HTTP has no authentication** of its own — bind it to localhost. MCP stdio trusts the local user.
9. **Secrets at rest:** exports and the audit DB are unencrypted files (0600 / 0700). The llama-server key is one
   shared key in the environment.
10. **False refusals:** the small-count output check looks at the outermost SELECT only, so a legitimate
    `COUNT(DISTINCT name)` written through a CTE is refused (the same query without the CTE passes).
11. **Not verified here:** the Docker image (base-image layers blocked), federation members (DuckDB extensions
    blocked), behaviour with the real models (not downloaded yet).

## Reporting

Security issues: contact the repository owner privately; do not open a public issue.
