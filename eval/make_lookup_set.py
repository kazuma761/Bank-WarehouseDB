"""Phase 3: generate the lookup evaluation set from the data itself (no question is ever put into a prompt).

    .venv/bin/python eval/make_lookup_set.py --source bird_demo --out eval/datasets/lookup_bird.jsonl

~60 items: six tools × four phrasing styles (formal, casual, typo, Hinglish) by id; statements with explicit months;
name lookups (unique names), ambiguous names (planted duplicates → needs_clarification), IDs that do not exist
(→ refused). Gold: route, tools + args, expected status, and expected rows (first tool) computed with our own
fixed lookup tools — i.e. what a correct answer must return.
"""
from __future__ import annotations

import argparse
import calendar
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bankql.config import load_config  # noqa: E402
from bankql.connectors import build_sources, jsonable  # noqa: E402
from bankql.tools import LookupTools  # noqa: E402

PHRASES = {
    "get_client": {"formal": "Please show the profile of client {id}.", "casual": "who's client {id}?",
                   "typo": "detials of clinet {id}", "hinglish": "client {id} ki details dikhao"},
    "get_accounts": {"formal": "List the accounts of client {id}.", "casual": "what accounts does client {id} have",
                     "typo": "acounts for client {id} pls", "hinglish": "client {id} ke saare accounts batao"},
    "get_cards": {"formal": "Which cards have been issued to client {id}?", "casual": "client {id} cards?",
                  "typo": "crads of client {id}", "hinglish": "client {id} ke paas kaunse cards hain"},
    "get_loans": {"formal": "Show the loans held by client {id}.", "casual": "any loans for client {id}?",
                  "typo": "lons of clinet {id}", "hinglish": "client {id} ke loans dikhao"},
    "get_orders": {"formal": "Show the standing orders on account {id}.", "casual": "standing orders for acct {id}",
                   "typo": "standng orders account {id}", "hinglish": "account {id} ke standing orders batao"},
    "get_statement": {"formal": "Show the recent statement of account {id}.", "casual": "latest transactions on account {id}",
                      "typo": "statment for acount {id}", "hinglish": "account {id} ka statement dikhao"},
}


def _rows(tr) -> list[list]:
    return [[jsonable(v) for v in r] for r in tr.rows]


def item(i, q, style, tool, args, expect, rows=None, note=""):
    return {"id": f"lk{i:03d}", "question": q, "style": style, "gold_route": "lookup",
            "gold_tools": [{"tool": tool, "args": args}], "expect": expect, "expected_rows": rows, "note": note}


def generate(src, seed: int = 7) -> list[dict]:
    rng, tools, out = random.Random(seed), LookupTools(src), []
    clients = [r[0] for r in src.run("SELECT client_id FROM client ORDER BY client_id", max_rows=10**6).rows]
    owners = [r[0] for r in src.run("SELECT client_id FROM disp WHERE type = 'OWNER' ORDER BY client_id", max_rows=10**6).rows]
    accounts = [r[0] for r in src.run('SELECT DISTINCT account_id FROM "order" ORDER BY 1', max_rows=10**6).rows]
    loan_clients = [r[0] for r in src.run("SELECT DISTINCT d.client_id FROM disp d JOIN loan l ON l.account_id = d.account_id "
                                          "WHERE d.type = 'OWNER' ORDER BY 1", max_rows=10**6).rows]
    card_clients = [r[0] for r in src.run("SELECT DISTINCT d.client_id FROM disp d JOIN card c ON c.disp_id = d.disp_id ORDER BY 1",
                                          max_rows=10**6).rows]
    pick = {"get_client": clients, "get_accounts": owners, "get_cards": card_clients, "get_loans": loan_clients,
            "get_orders": accounts, "get_statement": accounts}
    n = 0
    for tool, styles in PHRASES.items():  # 6 × 4 = 24 by id
        for style, tmpl in styles.items():
            ident = rng.choice(pick[tool])
            key = "account_id" if tool in ("get_orders", "get_statement") else "client_id"
            tr = getattr(tools, tool)(**({key: ident} if tool != "get_loans" else {"client_id": ident}))
            n += 1
            out.append(item(n, tmpl.format(id=ident), style, tool, {key: ident}, "ok", _rows(tr)))
    # statements for an explicit month (8)
    months = src.run("SELECT DISTINCT account_id, SUBSTR(CAST(date AS VARCHAR), 1, 7) AS ym FROM trans "
                     "WHERE date >= '1997-01-01' ORDER BY 1, 2", max_rows=10**6).rows
    for aid, ym in rng.sample(months, min(8, len(months))):
        y, m = int(ym[:4]), int(ym[5:7])
        d0, d1 = f"{ym}-01", f"{ym}-{calendar.monthrange(y, m)[1]:02d}"
        q = rng.choice(["statement for account {a} in {mon} {y}", "account {a} ka {mon} {y} ka statement",
                        "transactions on acct {a} during {mon} {y}"]).format(a=aid, mon=calendar.month_name[m], y=y)
        n += 1
        out.append(item(n, q, "dates", "get_statement", {"account_id": aid, "date_from": d0, "date_to": d1}, "ok",
                        _rows(tools.get_statement(aid, d0, d1))))
    # names: unique (10) and ambiguous (6)
    names = src.run("SELECT full_name, MIN(client_id), COUNT(*) FROM client_profile GROUP BY full_name ORDER BY full_name",
                    max_rows=10**6, allow_pii_output=True).rows
    unique = [(nm, cid) for nm, cid, c in names if c == 1]
    dupes = [nm for nm, _, c in names if c > 1]
    name_tmpls = [("show {n}'s accounts", "get_accounts"), ("{n} ke accounts dikhao", "get_accounts"),
                  ("profile of {n}", "get_client"), ("cards of {n}", "get_cards")]
    for nm, cid in rng.sample(unique, min(10, len(unique))):
        tmpl, tool = rng.choice(name_tmpls)
        n += 1
        out.append(item(n, tmpl.format(n=nm), "name", tool, {"client_name": nm}, "ok", _rows(getattr(tools, tool)(cid))))
    amb_tmpls = [("show {n}'s profile", "get_client"), ("{n} ka profile", "get_client"), ("accounts of {n}", "get_accounts")]
    for nm in (dupes * 6)[:6] if dupes else []:
        tmpl, tool = rng.choice(amb_tmpls)
        n += 1
        out.append(item(n, tmpl.format(n=nm), "ambiguous_name", tool, {"client_name": nm}, "needs_clarification",
                        note="planted duplicate name"))
    # relative periods (3) and two tools in one question (3), counted back from the source's as_of date
    from bankql.router import resolve_period

    for aid in rng.sample(accounts, 3):
        d0, d1 = resolve_period("last_3_months", src.as_of())
        n += 1
        out.append(item(n, f"last 3 months of transactions on account {aid}", "period", "get_statement",
                        {"account_id": aid, "period": "last_3_months"}, "ok", _rows(tools.get_statement(aid, d0.isoformat(), d1.isoformat()))))
    single = [r[0] for r in src.run("SELECT client_id FROM disp WHERE type = 'OWNER' GROUP BY client_id HAVING COUNT(*) = 1 "
                                    "ORDER BY 1", max_rows=10**6).rows]
    for cid in rng.sample(single, 3):
        n += 1
        it = item(n, f"show client {cid}'s accounts and last month's statement", "multi_tool", "get_accounts",
                  {"client_id": cid}, "ok", _rows(tools.get_accounts(cid)))
        it["gold_tools"].append({"tool": "get_statement", "args": {"client_id": cid, "period": "last_month"}})
        out.append(it)
    # ids that do not exist (6)
    top_c, top_a = max(clients), max(accounts + [0])
    for k in range(6):
        tool = ["get_client", "get_accounts", "get_orders"][k % 3]
        bad = (top_a if tool == "get_orders" else top_c) + 1000 + 37 * k
        key = "account_id" if tool == "get_orders" else "client_id"
        n += 1
        out.append(item(n, PHRASES[tool][["formal", "casual", "hinglish"][k % 3]].format(id=bad), "missing_id", tool, {key: bad}, "refused"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="bird_demo")
    ap.add_argument("--out", default=str(ROOT / "eval/datasets/lookup_bird.jsonl"))
    a = ap.parse_args()
    src = build_sources(load_config(ROOT / "config"))[a.source]
    items = generate(src)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("".join(json.dumps(i, default=str) + "\n" for i in items))
    from collections import Counter
    print(f"{len(items)} items -> {a.out}", dict(Counter(i["style"] for i in items)))


if __name__ == "__main__":
    main()
