# HOLD v0

## Purpose

An organisation that approves an action on the strength of a particular claim version enrolls that action at its own gate. A timely, admissible challenge pauses commitment and opens a review. The relying organisation owns the decision and bears the costs of its hold.

## Rules

1. **Enroll before commitment.** Record the organisation, action ID, immutable action target, claim type, issuer, claim ID, version and content digest. Pin the accepted challenger roles, objection grounds, private evidence intake, named reviewer, decision owner, escalation reviewer and review period. Every enrolled approval uses the same gate for its irreversible step. A change to the action target requires a new enrollment.
2. **Order receipt and commitment at the same gate.** A single durable local order covers challenge intake, decisions, relevant evidence and mandate updates, and commitment. Each event gets a local sequence and a trusted local time. In the replay the input clock uses integer ticks. The gate performs admission, records the receipt and opens any hold in one transaction. Commitment atomically checks the gate and records the simulated irreversible effect. If processes race at the same tick, the lower committed sequence wins. A send timestamp, private evidence deposit or human acknowledgement carries no place in that order.
3. **Admit against the exact reliance.** Require an authenticated, admitted challenger, an exact relying organisation and action, a match to its approved claim identity and version, an allowed ground and a reference accepted by that gate's private evidence intake for its reviewer. Intake checks routing and eligibility. The reviewer assesses the merits. For an otherwise admissible delivery, receipt is timely once approval exists and the receipt is durable before commitment. A fresh timely delivery creates an open hold, including when the action already has an open hold. Distinct holds each get their own receipt, reviewer and deadline. Evaluate a post-commit delivery against the basis recorded at commitment. An otherwise admissible delivery receives `LATE` with the commitment reference. Invalid input receives `REJECTED` with a reason. Receipt for a cancelled action reports `CLOSED`.
4. **Acknowledge locally, retry safely.** Both buyers may receive the same immutable challenge, identified by challenger and challenge ID and bound to the claim version and objection ground. Address each delivery to a specific organisation and action, with a local reviewer reference. After authenticating the submitter, an exact retry of an already recorded delivery returns its original receipt, including its original deadline and sequence. Reuse of that delivery key with changed content is rejected and logged. Return the receipt only after the gate transaction is durable. It identifies the receiving gate and reviewer assignment. A human acknowledgement may be recorded separately. One buyer's receipt, decision or clock carries no authority at another buyer's gate.
5. **Pause the enrolled commitment.** Any open hold blocks the irreversible effect. Private staging, evidence gathering, reversible preparation and unrelated actions can proceed. The relying organisation funds its own review and incremental waiting costs. In the shipment fixture that includes storage while acceptance is paused. The protocol imposes no waiting fee on the challenger or another buyer.
6. **Make delay reviewable.** Each admitted challenge has a named reviewer and a due tick equal to its receipt tick plus five in these fixtures. While the case is open, either the challenger or the action owner may file a delay petition with the named escalation reviewer and get a receipt. A petition leaves the due tick in force. At the due tick, record one overdue escalation per still-open case before processing any command at that tick. Use an explicit clock advance in replay. The escalation reviewer can request a decision or reassign the case to a currently authorised named reviewer with a recorded reason. Keep the original deadline. The hold stays active and the merits stay open until an authorised decision. Later resolution remains possible.
7. **Revalidate before resumption and commitment.** The decision owner can cancel the held action with recorded reviewer findings and a reason. Any outstanding cases close with the action-cancelled disposition. To resume, the owner submits a fresh approval and current findings from the designated reviewers covering every open challenge, including any that arrived during review. In one transaction the gate checks the exact action target, the current evidence and claim version, the reviewer and owner mandates, and coverage of the full open set. The approval binds the evidence and authority versions checked. A failed check leaves the holds open and records the refusal. A successful check records the findings, closes those holds and returns the action to `APPROVED`, where a fresh admissible challenge can still pause it. Commitment again checks for open holds and current evidence, approval and authority, using updates recorded in the gate's local order. An exact commit retry returns the initial result and cannot create a second effect.

### Receipt and decision record

Record gate and organisation IDs, action and claim identity, authenticated actor and role, challenge ID, local sequence and tick, result and reason code. When applicable, link the private evidence reference, named reviewer, due tick, petition, escalation, prior receipt, decision or commitment. A decision records the named reviewers, decision owner, evidence and approval versions checked, and a concise reason. Keep the evidence bytes and detailed findings with the designated reviewers. Share only the routing and outcome metadata with the relevant parties. The offline fixture models trusted identities and mandates as data.

## Synthetic enrollments

| Gate and target | Relied-on claim and eligible challenge | Decision owner | Reviewer | Escalation reviewer |
|---|---|---|---|---|
| AEGIS-TRACE, publish dataset `DS-7` to the public catalogue | Steward clearance `SC-7 v1`. Steward Iona Vale raises `DATASET_SCOPE_MISMATCH`. | Release manager Rowan Ellis | Release reviewer Ellis Chen | Review lead Ava Bell |
| North buyer, accept shipment batch `B42` | Lab report `LR-17 v1`. Inspector Sam Ortiz raises `BATCH_MISMATCH`. | Buyer lead Nia Ford | Quality reviewer Alex Reed | Review lead Jo Shah |
| South buyer, accept shipment batch `B42` | Same `LR-17 v1` and inspector challenge, delivered at South's gate. | Buyer lead Dev Malik | Quality reviewer Bea Singh | Review lead Theo Price |

In the fixtures, identify each claim by issuer, ID, version and a digest of its synthetic bytes. Use a distinct opaque private reference at each buyer.

- **AEGIS-TRACE.** The last stoppable point is immediately before the public catalogue transaction that makes `DS-7` visible. The staged dataset stays private during review. The private evidence shows that `SC-7 v1` covers `DS-6`. Corrected clearance `SC-7 v2` covers `DS-7` and supersedes v1.
- **Buyers.** Each buyer's last stoppable point is immediately before its own authoritative acceptance posting for `B42`. Quarantine, inspections and paperwork can continue. The private evidence shows that the lab report covers `B41`. North also has an unrelated approved order `O99`, with its own action and claim.

## Offline replay and expected outcomes

Every trace starts from fresh approved enrollments. Read successive operations at one gate in the order shown, even when their tick matches. Ticks are local fixture values. The replay requires no cross-organisation clock comparison.

### AEGIS-TRACE

| Tick | Event | Expected result |
|---|---|---|
| 10 | Receipt of steward challenge `C-A`, then a commit request at the same tick. | `ADMITTED`, due at 15, reviewer Ellis Chen. Commit is `BLOCKED_HOLD`. Public effects are zero. |
| 11 | Continue private preparation. | The dataset remains private and the hold stays open. |
| 12 | Record corrected `SC-7 v2` and a newer mandate revision in the gate's current records. | The update supplies a new review basis. The hold stays open. |
| 13 | Request resumption first with v1 and current mandates, then with v2 and a superseded owner mandate. | Both requests are refused. The first cites stale evidence, the second stale authority. The hold stays open. |
| 14 | Ellis Chen's current finding covers `C-A` on v2. Rowan Ellis supplies a fresh approval under the current mandate. Then commit and retry the same commit. | `RESUMED` with v2 bound to the approval. Commitment publishes `DS-7` once. Its retry returns the original commitment. |

### Two buyers, both timely

| Tick | Event | Expected result |
|---|---|---|
| 20 | Sam Ortiz delivers challenge `C-B` against `LR-17 v1` to North and South. | Each gate returns its own `ADMITTED` receipt, due at 25, and opens its own hold. |
| 21 | North commits unrelated `O99`. Retry the original challenge delivery to South. | `O99` commits once. South returns its original challenge receipt with the same due tick. Both `B42` acceptances are still paused. |
| 22 | Alex Reed substantiates the mismatch. Nia Ford cancels North's `B42` acceptance. | North is `CANCELLED`, with the reason recorded and zero `B42` acceptance effects. |
| 23 | Sam Ortiz files a delay petition at South. | South acknowledges the petition and routes it to Theo Price. The due tick stays at 25. |
| 25 | Advance South's clock to the deadline, then request commitment. | One `OVERDUE` event goes to Theo Price. Bea Singh's merits review is open. Commitment is `BLOCKED_HOLD`, with zero South acceptance effects. |

### Two buyers, one too late

| Tick | Event | Expected result |
|---|---|---|
| 30 | North admits `C-B`. At South, commit `B42` first, then deliver `C-B` at the same local tick. | North is held, due at 35. South commits once and returns `LATE` with its prior commitment reference. South opens zero holds. Backdating the sender timestamp gives the same result. An exact retry returns the original late receipt. |

### Boundary controls

- Reverse the equal-tick order at AEGIS-TRACE. Commitment succeeds once and the subsequent otherwise admissible objection gets a late receipt.
- Branch from AEGIS-TRACE after resumption and before commitment. A new challenge against v1 is rejected for the reliance mismatch. An admissible challenge to v2 pauses the action again. In a separate branch, revoke a mandate used by the resumed approval before commitment and check that the gate denies commitment.
- Admit a second distinct challenge while an action is held, then attempt resumption with findings covering only the first. The gate refuses. Both cases retain their original deadlines. A duplicate delivery creates zero additional cases.
- Present a wrong version or scope, an ineligible challenger, an unavailable reviewer route and changed content under an existing delivery key. Each is rejected and creates zero new holds.
- At the exact deadline, process the overdue event before a valid same-tick resolution. Record both in local order. Reassignment while overdue preserves the deadline and requires the replacement reviewer's current authority for later resolution.
- Replay a lost receipt response after its transaction, and interruption before a receipt transaction commits. The former returns the original durable receipt on retry. The latter leaves zero admitted holds until successful delivery. Repeat the interruption test at the commitment boundary and count durable effects.

## Milestone

Engineering runs the same gate code with the two workflow fixtures and returns a replay command, expected and observed results, and the ordered receipts, decisions and effects. Formal independently replays from clean state and challenges the timing and resolution rules. In this milestone the gate transaction itself represents publication or acceptance, so no real publication, shipment acceptance or evidence transfer is involved.
