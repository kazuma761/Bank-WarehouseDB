"""Plug in your own data as a new read-only source: import CSV / TSV / Parquet files into a local DuckDB file, or
connect an existing database by URL. Used by the /admin page (REST, admin keys only) and `bankql import|connect`.

Writes only: data/imports/<name>.duckdb (imports), config/sources.local.yaml (git-ignored),
config/semantic/local/<name>/<table>.yaml (generated skeletons; suspected PII flagged by column name) and one
BANKQL_SRC_<NAME>_URL line in .env (mode 600). A source is saved only when `check-source` has no FAIL line, so a
database user that can write is refused. Sources in config/sources.yaml can't be replaced or removed from here."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml
from sqlalchemy.engine import make_url

from bankql.cli import FAIL, check_source, init_source, read_only_checks
from bankql.config import LOCAL_SOURCES, local_sources
from bankql.connectors import Source, SourceConfig
from bankql.semantic import SemanticModel, load_semantic

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
READERS = {".csv": "read_csv", ".tsv": "read_csv", ".parquet": "read_parquet"}


class OnboardError(ValueError):
    """Bad input (name, file type, URL); the message is safe to show the caller."""


@dataclass
class Onboarded:
    name: str
    ok: bool
    tables: list[str] = field(default_factory=list)
    checks: list[tuple[str, str, str]] = field(default_factory=list)
    semantic_files: list[str] = field(default_factory=list)


def env_var(name: str) -> str:
    return f"BANKQL_SRC_{name.upper()}_URL"


def check_name(config_dir: Path, name: str) -> None:
    if not NAME_RE.match(name or ""):
        raise OnboardError("source name: lowercase letters, digits and _, starting with a letter (max 40)")
    base = yaml.safe_load((Path(config_dir) / "sources.yaml").read_text()) or {}
    if name in (base.get("sources") or {}) or name in (base.get("federations") or {}):
        raise OnboardError(f"{name!r} is defined in config/sources.yaml; pick another name")


def table_name(filename: str) -> str:
    stem, suffix = os.path.splitext(os.path.basename(filename or ""))
    if suffix.lower() not in READERS:
        raise OnboardError(f"unsupported file type {suffix or '(none)'!r}: use .csv, .tsv or .parquet (save Excel as CSV)")
    t = re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")[:60]
    if not t:
        raise OnboardError(f"can't make a table name from {filename!r}")
    return t if t[0].isalpha() else f"t_{t}"


def import_file(config_dir: Path, imports_dir: Path, name: str, filename: str, path: Path) -> tuple[Path, str]:
    """Load one file into table <file stem> of imports_dir/<name>.duckdb (replaced if it exists). Nothing else may
    hold that DuckDB file open (the Agent drops the source first)."""
    check_name(config_dir, name)
    entry = local_sources(config_dir).get(name)
    if entry and entry.get("origin") != "import":
        raise OnboardError(f"{name!r} is a connected database, not an import; pick another name")
    table = table_name(filename)
    imports_dir.mkdir(parents=True, exist_ok=True)
    db = (imports_dir / f"{name}.duckdb").resolve()
    try:
        con = duckdb.connect(str(db))
    except duckdb.Error as e:  # e.g. a running API (another process) has it open
        raise OnboardError(f"could not open {db.name}: {str(e).splitlines()[0][:160]} — import through /admin while "
                           "the API runs, or stop it first") from e
    try:
        con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM {READERS[os.path.splitext(filename)[1].lower()]}(?)',
                    [str(path)])
    except duckdb.Error as e:
        raise OnboardError(f"could not read {os.path.basename(filename)}: {str(e).splitlines()[0][:200]}") from e
    finally:
        con.close()
    return db, table


def _probe(name: str, url: str, tables: list[str] | None) -> Source:
    try:
        backend = make_url(url).get_backend_name()
    except Exception as e:  # noqa: BLE001
        raise OnboardError("not a valid SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host:5432/db") from e
    os.environ[env_var(name)] = url
    raw = {"url_env": env_var(name), "dialect": backend, "tables": tables or [], "semantic": []}
    src = Source(name, SourceConfig.model_validate(raw), SemanticModel(name=name))
    if tables is None:
        src.cfg.tables = sorted(src._reflect())
    return src


def inspect_url(name: str, url: str) -> tuple[str, list[str], list[tuple[str, str, str]]]:
    """Dialect, tables and read-only checks for a URL, before anything is saved."""
    prev = os.environ.get(env_var(name))
    try:
        src = _probe(name, url, None)
        try:
            return src.dialect, src.cfg.tables, read_only_checks(src)
        finally:
            src.engine.dispose()
    except OnboardError:
        raise
    except Exception as e:  # noqa: BLE001 — connection problems are an answer here, not a 500
        return "", [], [(FAIL, "connect", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")]
    finally:
        os.environ.pop(env_var(name), None) if prev is None else os.environ.__setitem__(env_var(name), prev)


def register(config_dir: Path, env_file: Path, name: str, url: str, *, origin: str, tables: list[str] | None = None,
             description: str = "") -> Onboarded:
    """Save `name` as a source after `check-source` passes. tables=None: every table the URL shows."""
    config_dir = Path(config_dir)
    check_name(config_dir, name)
    prev = os.environ.get(env_var(name))
    created: list[Path] = []
    res = Onboarded(name, ok=False)
    try:
        probe = _probe(name, url, tables)
        try:
            present = {t.lower(): t for t in probe._reflect()}
        finally:
            probe.engine.dispose()
        res.tables = [present.get(t.lower(), t) for t in probe.cfg.tables]
        missing = [t for t in res.tables if t.lower() not in present]
        if missing or not res.tables:
            res.checks = [(FAIL, "tables", f"not found: {', '.join(missing)}" if missing else "no tables to query")]
            return res
        sem_dir = config_dir / "semantic" / "local" / name
        for t in res.tables:  # one skeleton per table, generated once: your edits are kept on re-import
            out = sem_dir / f"{re.sub(r'[^A-Za-z0-9_]+', '_', t)}.yaml"
            if not out.exists():
                one = Source(name, probe.cfg.model_copy(update={"tables": [t]}), SemanticModel(name=name))
                init_source(one, out)
                one.engine.dispose()
                created.append(out)
            res.semantic_files.append(str(out.relative_to(config_dir)))
        entry = {"origin": origin, "description": description or f"{origin}ed data",
                 "url_env": env_var(name), "dialect": probe.cfg.dialect, "read_only": True, "tables": res.tables,
                 "semantic": res.semantic_files, "as_of": "today"}
        sc = SourceConfig.model_validate({**entry, "semantic": [str(config_dir / p) for p in res.semantic_files]})
        src = Source(name, sc, load_semantic(sc.semantic))
        try:
            res.checks = check_source(src)
        finally:
            src.engine.dispose()
        res.ok = not any(s == FAIL for s, _, _ in res.checks)
        if res.ok:
            _save_entry(config_dir, name, entry)
            _set_env_line(env_file, env_var(name), url)
        return res
    except OnboardError:
        raise
    except Exception as e:  # noqa: BLE001
        res.checks.append((FAIL, "connect", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"))
        return res
    finally:
        if not res.ok:
            for p in created:
                p.unlink(missing_ok=True)
            os.environ.pop(env_var(name), None) if prev is None else os.environ.__setitem__(env_var(name), prev)


def remove(config_dir: Path, env_file: Path, name: str) -> bool:
    """Forget a plugged-in source. Imported data files and semantic YAML stay on disk."""
    check_name(config_dir, name)
    if name not in local_sources(config_dir):
        return False
    _save_entry(Path(config_dir), name, None)
    _set_env_line(env_file, env_var(name), None)
    os.environ.pop(env_var(name), None)
    return True


def _save_entry(config_dir: Path, name: str, entry: dict | None) -> None:
    p = config_dir / LOCAL_SOURCES
    doc = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
    srcs = doc.setdefault("sources", {})
    srcs.pop(name, None) if entry is None else srcs.__setitem__(name, entry)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("# Written by BankQL (/admin, `bankql import|connect`). Sources here never override sources.yaml.\n"
                   + yaml.safe_dump(doc, sort_keys=False))
    tmp.replace(p)


def _set_env_line(env_file: Path, key: str, value: str | None) -> None:
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    lines = [x for x in lines if not x.startswith(f"{key}=")] + ([f"{key}={value}"] if value is not None else [])
    env_file.write_text("\n".join(lines) + "\n")
    os.chmod(env_file, 0o600)
