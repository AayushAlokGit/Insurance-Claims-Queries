# AppointmentScripts — DD-021 evidence

Two prototype implementations of appointment extraction, kept side-by-side
so the trade-off behind **DD-021** ("appointments use per-note +
reconciliation, not a whole-claim LLM call") is reproducible.

| Script | Method | What it does |
|---|---|---|
| `appts_via_whole_claim_llm.py` | One LLM call per claim, every note in the prompt | Asks the model to emit the canonical appointment list directly. No per-note step, no resolver. |
| `appts_via_reconciliation.py` | Per-note extractor → DD-019 reconciliation | Production-equivalent path. Per-note candidate generation, deterministic pre-cluster, per-cluster LLM. |

## Frozen outputs

The four committed JSONs are **frozen comparison artifacts**, generated
during DD-021's investigation against Gemini `gemini-2.5-flash-lite` in
late May 2026. They are not regenerated on every run and are *not*
expected to reproduce byte-for-byte — `temperature=0` is not
bit-deterministic, and the underlying model has been updated since.

| File | What it shows |
|---|---|
| `1-29RT-recon-appts-list.json` | Reconciliation-method output for claim 1 |
| `1-29RT-whole-appts-list.json` | Whole-claim-LLM output for claim 1 |
| `2-248KR-recon-appts-list.json` | Reconciliation-method output for claim 2 |
| `2-248KR-whole-appts-list.json` | Whole-claim-LLM output for claim 2 |

DD-021 cites these to support its claim that the whole-claim method
under-recalls attended visits by 27–42% and drops every missed/cancelled
event on the sample claims. The recon path is what production uses.

## Running them

```powershell
# Comparison harness; both scripts hit the live LLM provider.
py -3.12 -m uv run python scripts/AppointmentScripts/appts_via_whole_claim_llm.py --file sample_claim_notes/sample_claim_notes1.md
py -3.12 -m uv run python scripts/AppointmentScripts/appts_via_reconciliation.py --file sample_claim_notes/sample_claim_notes1.md
```
