"""Port of Mercer (https://github.com/baselanaya/Mercer) at commit d6f922f92c836aee68f56e2a67cb98813a205dd4.

MIT License — Copyright (c) the Mercer authors; full text in ./LICENSE (copied unmodified).

Ported modules: core/, db/ (sandbox, explain), schema/, prompts/, kernels/, inference/{base,llamacpp_backend}.
Not ported: inference/router.py + api_backend.py (cloud SDKs), app/, eval/, scripts/, db/connectors/, db/audit_store.py.

Patches (everything else is byte-identical apart from `from X` -> `from bankql.analytics.mercer.X` imports):
  1. core/logging.py   — level from BANKQL_MERCER_LOG_LEVEL (default WARNING) instead of the global
                          pydantic-settings singleton; logs to stderr; leaves the host's root logger alone.
  2. kernels/*.py      — torch/triton imported only when BANKQL_MERCER_GPU_KERNELS=1; CPU fallbacks otherwise.
  3. core/pipeline.py  — `backend` is required (ModelRouter removed); accepts a prebuilt `annotated_schema`,
                          an injected `sandbox`, and an optional `audit_store`.
Verify with:  diff -r third_party/mercer/core bankql/analytics/mercer/core   (etc.)
"""
