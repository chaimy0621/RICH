#!/usr/bin/env python3
"""RICH pre-run integrity check. Exit 0=no FAIL, 1=any FAIL.
Usage: python scripts/rich_check.py [--fix]
  --fix  auto-correct WEIGHTS_V2 sum if off due to float rounding
"""
from __future__ import annotations
import ast, re, sys, argparse
from pathlib import Path

ROOT = Path(__file__).parent.parent  # scripts/ -> RICH/
_out: list[tuple[str, str, str]] = []

def _ok(n: str, m: str = "passed") -> None: _out.append(("OK  ", n, m))
def _warn(n: str, m: str)         -> None: _out.append(("WARN", n, m))
def _fail(n: str, m: str)         -> None: _out.append(("FAIL", n, m))

def _read(rel: str) -> str: return (ROOT / rel).read_text(encoding="utf-8")
def _tree(rel: str) -> ast.Module: return ast.parse(_read(rel))

def _assign(tree: ast.Module, name: str) -> ast.expr | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node.value
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node.value
    return None

def _strlist(node: ast.expr | None) -> list[str] | None:
    if not isinstance(node, ast.List):
        return None
    out: list[str] = []
    for e in node.elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            out.append(e.value)
        else:
            return None
    return out

def _schema_cols(sql: str) -> set[str]:
    skip = {"PRIMARY","UNIQUE","CHECK","FOREIGN","CREATE","ON","INDEX","IF","NOT","CONSTRAINT"}
    cols: set[str] = set()
    for raw in sql.splitlines():
        line = raw.strip().rstrip(",").strip()
        if not line or line.startswith("--") or line.startswith(")"):
            continue
        tok = line.split()[0]
        if tok.upper() not in skip and re.fullmatch(r"[a-z_]\w*", tok, re.IGNORECASE):
            cols.add(tok)
    return cols

def _mig_cols(stmts: list[str]) -> list[str]:
    out = []
    for s in stmts:
        m = re.search(r"ADD\s+COLUMN\s+(\w+)", s, re.IGNORECASE)
        if m:
            out.append(m.group(1))
    return out

# ── 1. Schema integrity ──────────────────────────────────────────────────────
def check_schema() -> None:
    tree = _tree("data/storage.py")
    sn, cn, mn = (_assign(tree, x) for x in ("SCHEMA", "COLUMNS", "_MIGRATIONS"))
    if not isinstance(sn, ast.Constant) or not isinstance(sn.value, str):
        _fail("schema/SCHEMA", "SCHEMA not found or not a string literal"); return
    sc = _schema_cols(sn.value)
    cols = _strlist(cn)
    if cols is None:
        _fail("schema/COLUMNS", "COLUMNS not found or not a plain string list"); return
    seen: set[str] = set()
    dups = [c for c in cols if c in seen or seen.add(c)]  # type: ignore[func-returns-value]
    _fail("schema/duplicates", f"duplicates: {dups}") if dups else _ok("schema/duplicates")
    bad = [c for c in cols if c not in sc]
    _fail("schema/columns-in-schema", f"COLUMNS not in SCHEMA: {bad}") if bad \
        else _ok("schema/columns-in-schema")
    migs = _strlist(mn)
    if migs is None:
        _warn("schema/migrations-in-schema", "_MIGRATIONS not parseable"); return
    # Only daily_picks ALTERs belong to SCHEMA; research_universe ALTERs live in SCHEMA_RESEARCH.
    migs = [s for s in migs if re.search(r"ALTER\s+TABLE\s+daily_picks\b", s, re.IGNORECASE)]
    bad_m = [c for c in _mig_cols(migs) if c not in sc]
    _fail("schema/migrations-in-schema", f"migration cols not in SCHEMA: {bad_m}") if bad_m \
        else _ok("schema/migrations-in-schema")

# ── 2. Pipeline contract ─────────────────────────────────────────────────────
def check_buildrow() -> None:
    cols = _strlist(_assign(_tree("data/storage.py"), "COLUMNS"))
    if cols is None:
        _warn("pipeline/buildrow", "COLUMNS unreadable"); return
    br_keys: list[str] | None = None
    for fn in ast.walk(_tree("pipeline.py")):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_build_row":
            for node in ast.walk(fn):
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                    keys = [k.value for k in node.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                    if len(keys) == len(node.value.keys):
                        br_keys = keys
    if br_keys is None:
        _warn("pipeline/buildrow", "could not statically parse _build_row return dict"); return
    extra   = [k for k in br_keys if k not in cols]
    missing = [k for k in cols   if k not in br_keys]
    if extra or missing:
        _fail("pipeline/buildrow", "; ".join(
            ([f"extra: {extra}"] if extra else []) + ([f"missing: {missing}"] if missing else [])))
    else:
        _ok("pipeline/buildrow", f"{len(br_keys)} keys match COLUMNS")

# ── 3. Config sanity ─────────────────────────────────────────────────────────
def check_config(fix: bool = False) -> None:
    tc = _tree("config.py")
    for name in ("EXCLUDED_SECTORS", "COOLDOWN_DAYS", "TOP_N"):
        _ok(f"config/{name}") if _assign(tc, name) is not None \
            else _fail(f"config/{name}", f"{name} missing from config.py")
    w_node = _assign(_tree("scoring.py"), "WEIGHTS_V2")
    if not isinstance(w_node, ast.Dict):
        _fail("config/WEIGHTS_V2", "WEIGHTS_V2 not found or not a dict literal in scoring.py"); return
    vals: list[float] = []
    for v in w_node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, (int, float)):
            vals.append(float(v.value))
        else:
            _warn("config/WEIGHTS_V2", "non-literal value; skipping sum check"); return
    total = sum(vals)
    if abs(total - 1.0) <= 0.001:
        _ok("config/WEIGHTS_V2", f"sum={total:.6f}"); return
    if not fix:
        _fail("config/WEIGHTS_V2", f"sum={total:.6f} (≠1.0 ±0.001); run --fix to correct"); return
    path  = ROOT / "scoring.py"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    old, new = str(vals[-1]), str(round(1.0 - sum(vals[:-1]), 10))
    lines[w_node.values[-1].lineno - 1] = lines[w_node.values[-1].lineno - 1].replace(old, new, 1)
    path.write_text("".join(lines), encoding="utf-8")
    _ok("config/WEIGHTS_V2", f"--fix applied: {old} → {new}")

# ── 4. Report dependency check ───────────────────────────────────────────────
def check_report() -> None:
    cols = set(_strlist(_assign(_tree("data/storage.py"), "COLUMNS")) or [])
    if not cols:
        _warn("report/deps", "COLUMNS unreadable"); return
    src  = _read("report_today.py")
    refs = set(re.findall(r'pick\["(\w+)"\]', src)) | set(re.findall(r'pick\.get\("(\w+)"', src))
    bad  = sorted(r for r in refs if r not in cols)
    _fail("report/deps", f"report_today.py references unknown columns: {bad}") if bad \
        else _ok("report/deps", f"{len(refs)} column refs checked")

# ── 5. Scoring rules check ───────────────────────────────────────────────────
def check_scoring_rules() -> None:
    fkeys: set[str] | None = None
    for fn in ast.walk(_tree("factors.py")):
        if isinstance(fn, ast.FunctionDef) and fn.name == "extract":
            for node in ast.walk(fn):
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                    keys = {k.value for k in node.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                    if len(keys) == len(node.value.keys):
                        fkeys = keys
    if fkeys is None:
        _warn("scoring/rules", "could not parse extract() return dict from factors.py"); return
    r_node = _assign(_tree("scoring.py"), "RULES")
    if not isinstance(r_node, ast.Dict):
        _warn("scoring/rules", "RULES not parseable in scoring.py"); return
    rule_keys = [k.value for k in r_node.keys
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    missing = [k for k in rule_keys if k not in fkeys]
    _fail("scoring/rules", f"RULES metrics not in factors.extract(): {missing}") if missing \
        else _ok("scoring/rules", f"{len(rule_keys)} rule metrics checked")

# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="RICH pre-run integrity check")
    ap.add_argument("--fix", action="store_true",
                    help="auto-correct WEIGHTS_V2 sum if off due to float rounding")
    args = ap.parse_args()
    check_schema()
    check_buildrow()
    check_config(fix=args.fix)
    check_report()
    check_scoring_rules()
    any_fail = False
    for level, name, msg in _out:
        print(f"{level} [{name}]{f' — {msg}' if msg else ''}")
        if level.strip() == "FAIL":
            any_fail = True
    return 1 if any_fail else 0

if __name__ == "__main__":
    sys.exit(main())
