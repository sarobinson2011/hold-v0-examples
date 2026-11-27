"""Run the seven HOLD examples from local fixtures and check their fresh records."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def read_json(path):
    return json.loads(path.read_bytes())


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_inputs():
    for line in (ROOT / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        expected, name = line.split("  ", 1)
        if file_digest(ROOT / name) != expected:
            raise ValueError(f"Release file differs: {name}")


def verify_streams(name, directory, pattern, expected):
    paths = sorted(directory.rglob(pattern))
    lines = [f"{file_digest(p)}  {p.relative_to(directory).as_posix()}\n" for p in paths]
    digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    if {"files": len(paths), "sha256": digest} != expected[name]:
        raise ValueError(f"{name}: generated observation files differ from the expected records")
    return len(paths)


def pending_receipt(database):
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute("SELECT CAST(body AS BLOB) FROM journal WHERE seq = 3").fetchone()
    if row is None:
        raise ValueError("Missing pending challenge receipt 3")
    return row[0]


def run(out):
    # The two catalogue scenarios launch different one-store worker processes.
    sys.path.insert(0, str(ROOT / "src"))
    import catalogue_scenarios as catalogue
    import check_records
    import founding
    import revocation_scenarios as revocation
    from gate import digest

    founding_out = out / "founding"
    catalogue_out = out / "catalogue"
    revocation_out = out / "revocation"
    for directory in (founding_out, catalogue_out, revocation_out):
        directory.mkdir()

    traces = [FIXTURES / "traces" / f"{name}.json" for name in ("01-aegis", "02-buyers-timely")]
    founding.run(founding_out, traces, custom=True, compare=False)

    spec = read_json(FIXTURES / "gates.json")["aegis"]
    trial = read_json(FIXTURES / "trial.json")
    post, challenge = read_json(FIXTURES / "traces" / "05-aegis-commit-first.json")["steps"]
    catalogue.equal("posting command", (post["principal"], post["command"]),
                    (trial["owner"], trial["commit_command"]))
    catalogue.equal("challenge delivery", (challenge["principal"], challenge["command"]),
                    (trial["challenger"], trial["delivery"]))
    expectations = read_json(FIXTURES / "catalogue" / "expected.json")
    fixed = [catalogue.trace(catalogue_out, name, spec, trial, expectations[name])
             for name in ("block-first", "post-first")]
    catalogue.save(catalogue_out / "summary.json", fixed)

    restart = read_json(catalogue_out / "post-first" / "snapshots" / "restart" / "gate.json")
    request = restart["journal"][1]["body"]["request"]
    revocation.eq("fresh posting intent", request, read_json(FIXTURES / "post.json"))
    revocation.eq("full posting request digest", digest(request), revocation.REQUEST_DIGEST)
    expectations = read_json(FIXTURES / "expected.json")
    revoked = [revocation.history(revocation_out, name, expectations[name], spec, trial)
               for name in revocation.NAMES]
    revocation.save(revocation_out / "summary.json", revoked)
    check_records.audit(revocation_out, FIXTURES)

    expected = read_json(FIXTURES / "expected-streams.json")
    states_dir = founding_out / "observations"
    count = verify_streams("founding", states_dir, "*.jsonl", expected)
    count += verify_streams("catalogue", catalogue_out, "*.json", expected)
    count += verify_streams("revocation", revocation_out, "*.json", expected)

    for line in (states_dir / "states.jsonl").read_bytes().splitlines():
        state = json.loads(line)
        print(f"  {state['gate']} {state['action']}: {state['status']}, "
              f"holds={state['open']}, gate effects={state['effects']}")
    for row in fixed:
        print(f"  {row['history']}: gate {row['disposition']} #{row['final_seq']}, "
              f"listings={row['visible_DS7']}, holds={row['open_holds']}")
    blocked = pending_receipt(catalogue_out / "block-first" / "restart-gate.sqlite")
    posted = pending_receipt(catalogue_out / "post-first" / "restart-gate.sqlite")
    catalogue.equal("pending receipt bytes at restart", blocked, posted)
    print(f"  Pending #{json.loads(blocked)['seq']}: identical {len(blocked)} bytes at both restarts")

    transcript = read_json(catalogue_out / "post-first" / "transcript.json")
    steps = {item["step"]: item["observed"] for item in transcript if item["step"] in (17, 18, 19)}
    original = steps[17]
    catalogue.equal("receipt reconciled at the gate", steps[18]["catalogue_receipt"], original)
    catalogue.equal("receipt recovered on retry", steps[19]["receipt"], original)
    print(f"  Lookup, reconciliation and retry recovered {original['ref']}")
    for row in revoked:
        print(f"  {row['schedule']}: catalogue mandate #{row['mandate_seq']}, "
              f"{row['outcome']} #{row['decision_seq']}, listings={row['visible']}, "
              f"holds={row['open_holds']}")
    print(f"PASS: 7 examples. {count} fresh observation files matched the expected bytes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("results"),
                        help="new directory for journals, snapshots and SQLite databases")
    out = parser.parse_args().out
    verify_inputs()
    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"output already exists: {out}. Choose a new --out directory.")
    run(out)


if __name__ == "__main__":
    main()
