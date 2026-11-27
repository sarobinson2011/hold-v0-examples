# HOLD v0 examples

HOLD lets someone challenge the evidence behind an approved action while there is still time to stop it. Each organisation has a *gate* that records the objection and can pause the action for review. Suppose two buyers learn that the lab report for a shipment covers a different batch. Each buyer can pause its own acceptance while an unrelated order goes ahead.

The seven synthetic examples run offline. Two follow a publication and a shipment through review. Five use a separate catalogue to show when a challenge or a withdrawal of permission can still stop a post, and how to recover the original receipt if the post succeeds but the reply is lost.

[Download the verified release ZIP](https://github.com/sarobinson2011/hold-v0-examples/releases/download/v0-examples-1/HOLD-v0-Examples-Edition-1-Final.zip) · [Browse the Python source](examples/src/) · [View the v0-examples-1 release](https://github.com/sarobinson2011/hold-v0-examples/releases/tag/v0-examples-1)

**Project status.** Examples Edition 1 is complete. Further development is parked. A reproducible failure in this edition or a concrete new use case can prompt a follow-up.

## Clone and run

Start in a writable directory with Git and Python 3 available. The replay uses Python’s standard SQLite module and local files. Only the clone needs network access.

```sh
git clone https://github.com/sarobinson2011/hold-v0-examples.git
cd hold-v0-examples/examples
python3 -I -B run_examples.py --out results
```

The final line should read `PASS: 7 examples. 56 fresh observation files matched the expected bytes.` The journals, receipts and SQLite databases are in `results/`. Choose a new `--out` directory for each later run.

## What happens in each example

| Example | What the records show |
|---|---|
| 1. A publication is challenged | A steward questions the clearance for publishing DS-7. Publication pauses until a reviewer checks the revised evidence and the owner authorises it to resume. |
| 2. Two buyers receive the same objection | North cancels acceptance of batch B42 and goes ahead with the unrelated order O99. South escalates the objection at its deadline and keeps B42 on hold. |
| 3. A block reaches the catalogue first | The catalogue blocks the delayed post. The challenge opens a hold and no listing appears. |
| 4. A post succeeds but its reply is lost | The gate looks up the original posting receipt. The challenge is recorded as late. Retrying the post leaves exactly one listing. |
| 5. Permission is withdrawn before posting | The catalogue records the withdrawal first and refuses the delayed post. No listing appears. |
| 6. Posting comes before withdrawal | The catalogue posts under the permission in force at the time. After withdrawal, a retry recovers the original receipt and the single listing remains. |
| 7. The withdrawal notice is delayed | The gate already knows permission was withdrawn, but the catalogue has yet to receive the notice. The in-flight post reaches it first and creates one listing. |

A *pending* receipt confirms that the gate saved the challenge and can recover it after a restart. The same receipt appears in examples 3 and 4, even though their catalogue outcomes differ. The gate needs a matching catalogue receipt to tell which happened. [Guide to the output files](examples/README.md) · [HOLD rules](examples/HOLD-v0-rules-and-replay.md)

## Credit and licences

**sarobinson2011**. The code is licensed under MIT. The documentation and synthetic fixtures are licensed under CC BY 4.0. [Release notices](examples/RELEASE-NOTICES.md)

Release ZIP · 61,240 bytes · SHA-256 `d62a4b09d2a3135459011f174ffe91e251118d89232f30351642fcbce6ca375c`
