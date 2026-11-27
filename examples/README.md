# HOLD v0 · Seven offline examples

A steward challenges the clearance for DS-7. Two buyers receive the same objection to a shipment and decide independently. In the catalogue examples, a lost reply leaves the gate unsure whether publication stopped. You can inspect what each party knew by reopening its records.

## Run

Start in a new writable directory containing `HOLD-v0-Examples-Edition-1-Final.zip`. The quickstart uses a POSIX shell and Python 3 with its standard SQLite module. The tested environment was Linux x86-64 with CPython 3.12.14 and SQLite 3.53.1. All inputs are local.

```sh
(
  set -eu
  python3 -I -B -m zipfile -e HOLD-v0-Examples-Edition-1-Final.zip hold-examples
  python3 -I -B hold-examples/run_examples.py --out results
)
```

From an extracted source tree, run `python3 -I -B run_examples.py --out results`. Use a new output directory each time. The launcher checks the release files, runs the scenarios against new SQLite stores, and compares the fresh observations with the expected digests. A successful run ends with `PASS: 7 examples. 56 fresh observation files matched the expected bytes.`

## What to look for

Paths below start in `results/`. Each catalogue history starts afresh.

| Example | Observed result | Fresh evidence |
|---|---|---|
| Founding DS-7 | Iona’s challenge to SC-7 v1 pauses publication. Ellis records a finding on v2 at sequence 9. Rowan resumes at 10 and the gate records one simulated effect at 11. | `founding/observations/journal.jsonl`, trace `01-aegis` |
| Founding shipment | LR-17 covers B41. North cancels acceptance of B42 and commits the unrelated order O99. South escalates B42 at tick 25 and keeps its hold open. Neither buyer accepts B42. | `founding/observations/states.jsonl` and `journal.jsonl` in the same directory, trace `02-buyers-timely` |
| Challenge block first | Catalogue decision 1 blocks the post. The gate records `ADMITTED` at 4, opens one hold due at tick 18, and the catalogue has zero listings. | `catalogue/summary.json`, history `block-first` |
| Post first, acknowledgement lost | The catalogue has one listing. Lookup recovers its posting receipt. Gate 6 records `LATE` with zero holds, and the exact post retry returns the same receipt. | `catalogue/post-first/transcript.json`, steps 17, 18 and 19 |
| Mandate revocation first | Catalogue mandate receipt 1 makes Rowan inactive. Refusal 2 cites that revocation. Zero listings. | `revocation/summary.json`, schedule `revoke-first` |
| Post before gate revocation | Posting receipt 1 and the listing commit together under Rowan’s active revision 1. The posting reply is lost. The catalogue applies revocation at 2. One listing. | Same summary, schedule `post-first` |
| Gate revokes first, notice delayed | At step 6 the gate already has inactive revision 2. The catalogue still has active revision 1 and zero listings. At step 7 it posts once before the notice arrives. | `revocation/gate-first-notice-delayed/snapshots/gate-revoked-before-post.json` and `transcript.json` in the schedule directory |

**What a pending receipt means.** Gate receipt 3 at tick 10 establishes that the eligible challenge to intent 2 reached the gate durably. Its 1,073 stored bytes are identical in the two fixed-mandate restart databases. One catalogue has already blocked the post. The other has already posted. Missing lookup evidence or a reply for another request leaves the gate pending, with no fresh post. A block matching the exact request and challenge permits admission. A posting matching the request and original approval permits a late receipt. The launcher prints the recovered posting reference so you can follow it through lookup, reconciliation and retry.

**Scope.** The two founding cases use effects recorded by the organisation’s own gate. The later five histories use rows in a separate simulated catalogue. The examples exercise trusted synthetic identities, fixture ticks, durable local transaction order and recovery after process interruption. The executable results apply to these seven examples. External publication, shipment, authentication, network timing and physical power-loss durability require other evidence.

**Synthetic reviewer material.** The files `fixtures/private/aegis.json`, `fixtures/private/north.json` and `fixtures/private/south.json` contain example claim bytes, objections and findings. The founding runner checks each gate’s claim digests and case references when it starts. The reviewer function checks assignment and scope before returning a referenced item inside the simulation. Anyone who receives this source archive can read the three files.

## Credit and licences

The public project credit is **sarobinson2011**. Code is licensed under MIT. Documentation and synthetic fixtures are licensed under CC BY 4.0. The release includes the full licence texts and a suggested attribution. [Release notices](RELEASE-NOTICES.md)
