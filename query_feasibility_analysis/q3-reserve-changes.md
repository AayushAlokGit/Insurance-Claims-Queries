# Q3 — How many times did our reserve change? By how much?

## 1. What the question is really asking

Two numbers per claim (and per bucket):

- **Count** of reserve change events.
- **Magnitude** — by how much each change moved the reserve, and the total
  net movement from initial to current.

The bucket dimension matters: the brief asks "the reserve" (singular), but
reserves are tracked across multiple buckets (`Indemnity (1) Medical
Details`, `Indemnity (2) Lost Time`, `Expense-Litigation`). Collapsing
across buckets hides operationally meaningful signal.

## 2. Facts required

`reserve_change` events with:

- `bucket` — canonicalized as `<coverage> (<n>) <sub_bucket>`; samples
  observe 3 distinct values (see §3 table) but the space is
  `coverage × sub_bucket` and may expand in larger corpora
- `previous_amount` — derived; the prior value within the same bucket
- `new_amount` — extracted directly
- `delta` — `new_amount - previous_amount`
- `event_date` — from the note timestamp (down to the minute — same-minute
  paired updates are common; see §3)
- `author` — from `Noted By` (operational context, not query-required)

## 3. Where the facts live

`Activity: Reserving` notes. Strict template, every time. Verified against
both sample files (13 entries: 9 in claim 1, 4 in claim 2). The form is:

```
<coverage> for (<n>) <sub_bucket> Changed to $<amount>
```

The 2-D coverage × sub-bucket space observed in the samples:

| Coverage | Sub-bucket | Count | Example |
|---|---|---|---|
| `Indemnity` | `(1) Medical Details` | 4 | claim 1 L396, L670, L716; claim 2 L140 |
| `Indemnity` | `(2) Lost Time` | 5 | claim 1 L392, L666, L712; claim 2 L133, L839 |
| `Expense-Litigation` | `(1) Medical Details` | 3 | claim 1 L99, L323, L596 |
| `Expense-Litigation` | `(1) Medical Details` (claim 2) | 1 | claim 2 L846 |

A regex captures coverage, sub-bucket, and amount. The note timestamp gives
the date. No prose interpretation required, ever.

**Observation — paired same-minute updates.** Reserve changes come in
pairs: same author, same minute, both `Indemnity` buckets updated together
(claim 1 L392+L396, L666+L670, L712+L716; claim 2 L133+L140, L839+L846).
This is operationally meaningful (adjusters review both buckets together)
and matters for tie-breaking in the SQL ordering — see §7.

**Observation — Resolution Strategy restatements use a different format.**
Resolution Strategy notes summarize reserves in narrative form, e.g.
claim 2 L32: `"Reserves: Medical remaining $32,248.62; Indemnity remaining
$233,145.82"`; L174: `"Medical $32,800; Indemnity $268,830.00 Per POA
recommendation"`. These are *remaining balances* or *plan amounts*, **not
change events**, and the Reserving regex correctly does not match them. A
well-meaning extension to catch them would double-count. Reserve changes
live **exclusively** in `Activity: Reserving` notes.

## 4. Extraction approach — pure rule, no LLM

This is the **anchor of the system's credibility**. Financial numbers are
where a small error destroys trust in everything else. The rule:

```regex
^(?P<coverage>Indemnity|Expense-Litigation)\s+for\s+\((?P<num>\d+)\)\s+
 (?P<sub_bucket>[A-Za-z\s\-]+?)\s+Changed to\s+\$(?P<amount>[\d,]+\.\d{2})
```

The `coverage` alternation **must** spell out `Expense-Litigation`, not
just `Expense` — a `\s+for` after a bare `Expense` won't match
`"Expense-Litigation for ..."` and would silently drop 3 of 13 sample
entries (23%) of reserve changes. The non-greedy `+?` on `sub_bucket`
prevents it from eating `"Changed to"`.

Canonicalize to `<coverage> (<n>) <sub_bucket>` form, e.g.:
- `Indemnity (1) Medical Details`
- `Indemnity (2) Lost Time`
- `Expense-Litigation (1) Medical Details`

Strip commas from the amount and parse as decimal. Done — Q3 is answered
with zero LLM calls.

**Why never an LLM here, even partially:**
- The format is templated; an LLM adds variance with no benefit.
- LLMs occasionally mis-read digits (`321,014` vs `321,041`). Acceptable for
  prose, unacceptable for money.
- Reproducibility is non-negotiable for financial reporting.

## 5. Edge cases & ambiguities

| Situation | Decision |
|---|---|
| First reserve set ever for a bucket | Treated as a change from $0. The canned function reports it; documentation states the convention. (Alternative — "exclude initial set" — is also defensible; pick one and commit) |
| Same amount restated (no actual change) | Resolver drops it — `delta = 0` is not a "change" |
| Two reserve changes on the same day, same bucket | Both count; ordered by note timestamp (down to the minute) |
| Bucket name slightly different across notes | Canonicalize on parse; deviations are a data-quality flag, not silent merge |
| Currency other than USD | Out of scope for MVP — flag if encountered |
| Reserve "transferred" between buckets (rare) | Two events: a decrease in one bucket and an increase in another — falls out naturally from per-note extraction |

The `delta = 0` rule deserves emphasis. Resolution Strategy snapshots
sometimes restate the current reserve verbatim. Without the rule, those
inflate the count.

## 6. Computation

Counts and per-change deltas:

```sql
WITH ordered AS (
  SELECT
    claim_id,
    json_extract(attributes, '$.bucket')         AS bucket,
    json_extract(attributes, '$.new_amount')     AS new_amount,
    LAG(json_extract(attributes, '$.new_amount'))
      OVER (PARTITION BY claim_id,
                         json_extract(attributes, '$.bucket')
            ORDER BY event_date)                 AS prev_amount,
    event_date
  FROM event
  WHERE event_type = 'reserve_change'
)
SELECT
  claim_id,
  bucket,
  COUNT(*)                                                  AS num_changes,
  SUM(new_amount - COALESCE(prev_amount, 0))                AS net_movement,
  MAX(ABS(new_amount - COALESCE(prev_amount, 0)))           AS largest_swing
FROM ordered
GROUP BY claim_id, bucket;
```

Canned function: `reserveChangeSummary(claimId): {bucket, count, net, swings[]}[]`.
The per-change array (with date + delta) is what an adjuster actually wants
to look at — the summary alone is too lossy.

## 7. Reliability assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Regex mismatch on a malformed Reserving note | Very low | Unit tests cover all sample forms; log unmatched lines as data-quality warnings |
| Window function ordering wrong (ties) | Low | Order by `(event_date, note_index_within_file)` to break ties deterministically |
| Initial-set convention misunderstood | Medium (consumer-side) | Documented in function output and DESIGN.md |
| Bucket fragmentation | Low | Canonicalization on parse |

**Corpus-scale concern:** none material. This is the most reliable query in
the system by a wide margin. If Q3 produces a wrong number, it's a
deterministic bug (and therefore fixable for good), not a probabilistic
miss.

## 8. Design implications

Q3 is what justifies the "rules wherever possible" half of the hybrid
extraction tradeoff (DD-005, §5.4). Without Q3, the system could be all-LLM
and limp along. Q3 makes the **case** for hybrid: you cannot put exact
financial data through a non-deterministic component if you want anyone to
trust the numbers.

It also drives:

- The `extraction_method` debug column on `Event` — Q3 events should always
  read `"rule"`; if one ever shows `"llm"`, that's a regression.
- A general principle: any new event type whose attributes are **exact
  values from templated notes** (codes, dollar amounts, dates with a
  literal anchor) gets a rule extractor, not an LLM extractor. The LLM is
  reserved for prose.

## 9. Injury vs. illness applicability

**Q3 is the most claim-type-agnostic of the four.** `Activity: Reserving`
notes are system-generated and identical in shape regardless of whether the
underlying claim is an injury or an occupational illness — the regex, the
bucket vocabulary, and the delta math all carry over with zero changes.

What *does* differ at the corpus level is the **shape of the reserve trajectory**:

- **Injury claims** typically settle into reserves quickly — a few large
  changes early as the medical picture clarifies, then a long tail of
  smaller updates as treatment progresses.
- **Illness claims** often have a *delayed initial reserve* (compensability
  must be established first via causation opinion) followed by **stepwise
  increases** as exposure scope and population at risk is understood. Some
  toxic-tort claims see reserves grow for years.

This is interesting *as a corpus query* — "reserve volatility by
`claim_type`" is the kind of insight the brief explicitly asks for — but it
requires zero changes to Q3's extraction. The query layer reads the
existing `claim_type` column and groups; the underlying event data is the
same. This is exactly the design property we wanted: schema neutrality
where the data is genuinely neutral.
