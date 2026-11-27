"""Fixture-only reviewer view. The filesystem operator is trusted for this offline replay."""
import json
import sqlite3
from contextlib import closing

from representation import load_canonical


def read_case_reference(path, private_directory, principal, action, case_seq, ref):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        s = load_canonical(db.execute("SELECT body FROM local_state WHERE id=1").fetchone()[0])
    a = s["actions"].get(action, {})
    case = a.get("cases", {}).get(str(case_seq))
    if not (case and case["reviewer"] == principal and s["actors"].get(principal, {}).get("active")):
        raise PermissionError("current designated reviewer required")
    if ref == case["ref"]:
        section = "intake"
    elif any(f["private_ref"] == ref and f["case"] == case_seq and f["actor"] == principal
             for f in a["findings"].values()):
        section = "findings"
    else:
        raise PermissionError("reference outside this review")
    vault = json.loads((private_directory / (s["gate"] + ".json")).read_text())
    return vault[section][ref].encode()
