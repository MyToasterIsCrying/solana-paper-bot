#!/usr/bin/env python3
"""Persist the SQLite ledger in git as deterministic, diff-friendly SQL text.

Layout (all regenerated on every dump; unchanged files produce no git diff):
  ledger/schema.sql                  CREATE TABLE/INDEX statements
  ledger/state.sql                   account, positions, fills, sqlite_sequence (small, mutable tables)
  ledger/<table>/<YYYY-MM-DD>.sql    append-only history, one file per UTC day (keeps every file far below GitHub's 100 MB limit)
safety_cache is NOT persisted: it is a 30-minute RugCheck cache, not history.

  python3 tools/ledger_io.py restore   # ledger/ -> ledger.db (working copy, git-ignored)
  python3 tools/ledger_io.py dump      # ledger.db -> ledger/
  python3 tools/ledger_io.py verify    # dump to temp -> restore -> compare every ledger table row-for-row with ledger.db
"""
import os, sys, sqlite3, tempfile, shutil, glob
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "ledger.db")
OUT = os.path.join(ROOT, "ledger")
EXCLUDE = {"safety_cache"}
PARTITIONED = {"runs": "started_at", "scans": "ts", "evaluations": "ts", "skips": "ts", "errors": "ts",
               "equity_snapshots": "ts"}

def _inserts(con, table, where="", args=()):
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    expr = "||','||".join(f'quote("{c}")' for c in cols)
    for (vals,) in con.execute(f'SELECT {expr} FROM "{table}" {where} ORDER BY rowid', args):
        yield f'INSERT INTO "{table}" VALUES({vals});'

def dump(db=DB, out=OUT):
    con = sqlite3.connect(db)
    tmp = out + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    schema = [r[0] + ";" for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "AND tbl_name NOT IN ({}) ORDER BY type DESC, name".format(",".join("?" * len(EXCLUDE))), tuple(EXCLUDE))]
    open(os.path.join(tmp, "schema.sql"), "w").write("\n".join(schema) + "\n")
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    state = []
    for t in tables:
        if t in EXCLUDE:
            continue
        if t in PARTITIONED:
            col = PARTITIONED[t]
            days = [r[0] for r in con.execute(f'SELECT DISTINCT COALESCE(substr("{col}",1,10),"undated") FROM "{t}" ORDER BY 1')]
            os.makedirs(os.path.join(tmp, t))
            for d in days:
                if d == "undated":
                    rows = list(_inserts(con, t, f'WHERE "{col}" IS NULL'))
                else:
                    rows = list(_inserts(con, t, f'WHERE substr("{col}",1,10)=?', (d,)))
                open(os.path.join(tmp, t, f"{d}.sql"), "w").write("\n".join(rows) + "\n")
        else:
            state += list(_inserts(con, t))
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        state.append("DELETE FROM sqlite_sequence;")
        state += [f"INSERT INTO sqlite_sequence VALUES('{n}',{s});" for n, s in
                  con.execute("SELECT name, seq FROM sqlite_sequence WHERE name NOT IN ('safety_cache') ORDER BY name")]
    open(os.path.join(tmp, "state.sql"), "w").write("\n".join(state) + "\n")
    con.close()
    shutil.rmtree(out, ignore_errors=True); os.replace(tmp, out)

def restore(src=OUT, db=DB):
    for ext in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(db + ext):
            os.remove(db + ext)
    con = sqlite3.connect(db)
    con.executescript(open(os.path.join(src, "schema.sql")).read())
    con.execute("BEGIN")
    for t in sorted(PARTITIONED):
        for f in sorted(glob.glob(os.path.join(src, t, "*.sql"))):
            for line in open(f):
                if line.strip():
                    con.execute(line)
    for line in open(os.path.join(src, "state.sql")):
        if line.strip():
            con.execute(line)
    con.commit(); con.close()

def compare(a, b):
    ca, cb = sqlite3.connect(a), sqlite3.connect(b)
    for (t,) in ca.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if t in EXCLUDE:
            continue
        ra = ca.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()
        rb = cb.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()
        if ra != rb:
            raise SystemExit(f"MISMATCH in {t}: {len(ra)} vs {len(rb)} rows")
        print(f"  {t}: {len(ra)} rows identical")
    sa = ca.execute("SELECT * FROM sqlite_sequence WHERE name!='safety_cache' ORDER BY name").fetchall()
    sb = cb.execute("SELECT * FROM sqlite_sequence ORDER BY name").fetchall()
    if sa != sb:
        raise SystemExit(f"MISMATCH in sqlite_sequence: {sa} vs {sb}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "dump":
        dump()
    elif cmd == "restore":
        restore()
    elif cmd == "verify":
        d = tempfile.mkdtemp(); o = os.path.join(d, "ledger"); t = os.path.join(d, "x.db")
        dump(DB, o); restore(o, t); compare(DB, t); print("verify OK")
    else:
        raise SystemExit(__doc__)
