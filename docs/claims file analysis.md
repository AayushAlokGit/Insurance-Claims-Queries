# Claims File Analysis

A comprehensive inventory of **all** information contained in the two sample
workers' comp claim note files (`#1-29RT`, `#2-248KR`). The goal here is to
catalog everything that *could* be extracted — independent of any particular
query — so the data model and parser can be designed against the full surface
area of the source material.

> A later document will discuss **how** to use this information to answer
> specific questions (starting with the four sample queries). This file is
> deliberately scoped to *what exists in the data*, not *what we do with it*.

---

## 1. File & entry structure

Both files are **chronological claim notes**, ordered **newest entry first**.

### 1.1 Claim-level header
| Field | Claim 1 | Claim 2 |
|---|---|---|
| Claim # | `1-29RT` | `2-248KR` |
| Account # | `2100000000 - Company WW` | `0005 - Group W` |
| Insurer | "Dedicated Insurance Company" (in signature) | — |

### 1.2 Per-entry fields
Every note entry exposes:
- **Date / timestamp** — `MM/DD/YYYY HH:MMam/pm CT` (timezone always CT)
- **Activity** — one of a small controlled set (see 1.3)
- **Noted By** — author initials or role
- **Note** — free-text body (the bulk of the content)

### 1.3 Activity types (controlled vocabulary)
- **Resolution Strategy** — periodic full-claim summary snapshots
- **Contact** — emails / calls / texts with claimant, employer, providers
- **Investigation** — medical record reviews, appointment notes, surveillance, index reports
- **Reserving** — financial reserve changes

### 1.4 Authors / roles (`Noted By`)
- Adjuster / case manager initials: `M.H.`, `R.N.`, `B.S.`, `M.P.`
- System / automated: `Claims Adapter`, `System User`
- Anonymized / unknown: `X`
- Roles referenced in bodies: TCM (telephonic case manager), FCM (field case
  manager), CA (claims adjuster), Program Director, HR Director.

---

## 2. Claimant / employee information

- **Name** — anonymized in samples (`Patient A`, `Claimant`, `IE`, `EE`, `IW`)
- **Aliases / other names used**
- **Personal identifiers referenced** — SSN, DOB (values masked)
- **DOB** — e.g. `MM/DD/1978` (year only in samples)
- **Age** — stated at multiple points (46, 47 in claim 1; 49 in claim 2)
- **Sex / gender**
- **Height / Weight / BMI** — e.g. `5'2" / 205 lbs / 37.5 - obese`
- **Job title / occupation** — distribution worker, order picker, warehouse
  associate, (later) scheduling coordinator
- **Job duties / description** — narrative + formal job description / JDQ
- **Date of hire** — e.g. `4/14/2022`
- **Employment type** — full-time / part-time
- **Language / communication needs** — Spanish-speaking, translator required
- **Preferred contact method** — text / call / email
- **Social history** — marital status, dependents, tobacco/alcohol use, hobbies,
  home layout (stairs, single-story), vehicle type
- **Comorbidities / medical history / prior injuries** — explicitly captured
  (often "denies")

## 3. Employer / insured information

- **Employer name** — e.g. Apex Distribution Co., Employer Inc.
- **Employer contacts** — HR Director, Benefits Coordinator
- **Work location** — e.g. Warehouse Location D
- **Job description & physical requirements** — e.g. "carry/push/pull min 50 lbs"
- **JDQ** (job demands questionnaire) — completed / requested
- **Light-duty accommodation stance** — can / cannot accommodate, willingness to
  use RTW programs

## 4. Loss / incident

(Both samples are injury claims; "loss" is the loss-neutral term per DD-010
that also covers occupational illness — date of diagnosis / last exposure.)

- **Date of loss** — `DOL` (also seen as `DOI` / "date of injury" in
  injury-claim prose) — `12/21/2024`, `12/30/2024` in the samples
  - *Note:* one stray inconsistent date appears (`DOH 4/22/2024`) — data quality flag
- **Jurisdiction / state** — NJ, SC
- **Mechanism of injury** — narrative ("tripped on a rubber floor mat…",
  "reaching to retrieve product…")
- **Description of loss (FOL)** — formal restated version
- **Injured / compensable body parts** — head, neck, shoulder, knees, low back…
- **Loss of consciousness** — asked / recorded
- **Witness information** — witness statements (RK, TE), supervisor account
- **Incident / manager report** — time of incident, who summoned help, 911 called
- **Time of injury**

> **Scope note — injury vs. illness.** Both sample claims are *injury* claims
> (discrete trauma events). Workers' comp also covers **occupational illness /
> disease** (repetitive-motion conditions like carpal tunnel, exposure-based
> disease like silicosis or mesothelioma, hearing loss, occupational dermatitis,
> etc.). Illness claims look different in the data: no single date of injury
> but a *date of diagnosis* / *date of last exposure*; an *exposure history*
> instead of a mechanism narrative; ICD-10 codes drawn from `J` / `L` / `H` /
> `G` ranges rather than `S` / `M`; a heavy reliance on **causation opinions**
> linking the condition to the workplace. The inventory above reflects the
> injury-claim surface area; an illness-claim corpus would add exposure-
> history, diagnosis-date, and causation-opinion fields.

## 5. Diagnosis

- **ICD-10 codes** — e.g. `S14.129`, `M48.02`, `M54.5`, `M54.2`, `R53.1`,
  `M51.369`, `M47.816`, `M53.3`
- **Diagnosis descriptions** — paired with codes
- **Diagnosis evolution** — diagnosis changes over time (lumbar strain → lumbar
  radiculopathy / L5-S1 disc protrusion; cord syndrome refinements)

## 6. Providers & care team

- **Treating physicians** — name, specialty, clinic
  - Claim 1: Dr. Harmon (Spine), Dr. Vega (Neurology), Dr. Sinclair
    (Ophthalmology), Dr. Ruiz (trauma surgeon)
  - Claim 2: Dr. Caldwell (neurosurgeon / ortho), Dr. Farano (pain management)
- **Clinics / facilities** — Orthopedic & Spine Associates, Regional Medical
  Center, Valley PT Group, Spine & Neurology Group, Occupational Health Clinic,
  Parkview Rehab Center, imaging centers, etc.
- **Specialties** — spine, neurology, ophthalmology, PT, occupational therapy/FCE,
  pain management
- **Provider role** — treating vs. IME vs. consult

## 7. Appointments & scheduling events

- **Appointment scheduled events** — "scheduled for", scheduling notices with
  date/time
- **Appointment attended** — "Date of Appointment", FCM (field case manager)-attended visits
- **Next office visit (NOV)** — forward-looking
- **Attendance outcomes** — attended / no-show / cancelled / rescheduled
- **Appointment type** — office visit, follow-up, IME, evaluation, procedure
- **Scheduling friction** — transportation issues, provider availability
  (Tue/Thu only), authorization delays

> Caution: appointment dates inside a note are frequently **future-dated**
> relative to the note's own timestamp — a scheduled visit, not an attended one.

## 8. Medical treatment & clinical detail

- **Surgical interventions** — procedure, date, CPT codes (e.g. ACDF C4-5/C5-6
  on 12/22/24, codes 22551/22552)
- **Imaging / diagnostics** — MRI, CT, X-ray, EMG/NCS — with date, facility,
  findings, and IMPRESSION text
- **Diagnostic scheduling notices** — requested study, CPT code, state fee /
  estimated cost savings
- **Physical therapy** — frequency (e.g. 2x/week x6), session notes, HEP,
  clinical reviews, subjective/objective/assessment
- **Medications** — prescribed drugs (gabapentin, tizanidine, naproxen,
  cyclobenzaprine, lidocaine patches, etc.)
- **Injections / procedures** — MBB, RFA, ESI, para-facet injection
- **FCE** — functional capacity evaluation: ordered, scheduled, completed, results
- **DME / assistive devices** — AFOs, walker, rollator, wheelchair, cervical collar
- **Treatment summaries** — long narrative recaps of the medical course
- **Hospitalization** — admission, length of stay, discharge planning
- **Pain ratings** — numeric pain scores (e.g. 7/10)
- **Functional status** — ambulation, gait, ROM, strength, ADLs

## 9. Work status & return to work

- **Current work status** — OOW / off work, modified duty, light duty, full duty,
  MMI
- **Work restrictions** — lifting limits, sit/stand option, no bend/twist/climb,
  sedentary, position-change frequency
- **RTW date** — actual return-to-work date
- **RTW offer** — light/modified duty offer by employer, effective date
- **Accommodation** — employer able/unable to accommodate restrictions
- **RTW programs** — ReEmployability, transitional / vocational programs
- **MMI** — maximum medical improvement, by specialty, with date

## 10. Financial — reserves, benefits, costs

- **Reserve buckets** — Indemnity "(1) Medical Details", Indemnity "(2) Lost
  Time", Expense-Litigation
- **Reserve changes** — "Changed to $X" events with timestamp, author, rationale
- **Reserve breakdown / line items** — detailed cost build-ups (hospital, surgery,
  PT, MRIs, FCM (field case manager) / TCM (telephonic case manager),
  transportation, translation, attorney fees, etc.)
- **AWW** — average weekly wage (e.g. $803.60, $1,192.00)
- **CR** — compensation rate (e.g. $562.52, $794.67)
- **NLE** — value present (`34.92`) — meaning to confirm
- **Benefit types** — TTD, TPD, PPD, voc, IR (impairment rating)
- **Benefit payment status** — "paid through" dates, missed payments, diary lapses
- **Max / min weekly rate** — statutory rate caps
- **Permanency / impairment** — % of body parts, partial-total %, weeks of award
- **Bills / EOBs / charges** — medical bills, MRO record-request fees

## 11. Legal / litigation

- **Representation** — claimant counsel and defense counsel (names, firms)
- **Claim petition** — petition for permanency benefits filed
- **Compensability** — conditional denial, investigation, acceptance, legal opinion
- **Court activity** — judge, hearings, pre-trial conferences, adjournments
- **Settlement / permanency** — stipulation drafted/executed, order entered, award
- **Statutory forms** — e.g. SC Form 17 (to terminate TTD)
- **Jurisdiction-specific rules** — e.g. SC is an MMI state

## 12. Case management & claim handling

- **TCM (telephonic case manager)** — assignment / reassignment
- **FCM (field case manager)** — assignment, appointment attendance
- **Adjuster / CA** — assignment, reassignment, large-loss team transfer
- **Plan of action** — POA / "Plan to resolve the claim" / action plans
- **Claim reviews** — periodic claims reviews
- **Administrative actions** — records requests, authorizations, transportation &
  translation arrangements, SLA / support-ticket tracking

## 13. Investigation / SIU

- **SIU investigation** — fraud investigation, outcome
- **Surveillance** — field surveillance reports (dates, hours, observations,
  recommendations)
- **Social media investigation** — OSINT profile findings per platform
- **State medical canvas**
- **Index reports** — ISO/index report availability notices
- **Symptom amplification / credibility concerns** — clinician-noted

## 14. Disability guidelines (ODG)

- **ODG anticipated disability** — Target / Typical / Maximum day counts with
  projected calendar dates
- **Benchmark comparisons** — actual course vs. ODG expectation (e.g. 25-day
  hospitalization vs. 1 night recommended)
- **Estimated RTW date** — guideline-derived projection

## 15. Communications log

- **Channel** — email, phone call, text, voicemail, fax
- **Direction & parties** — between adjuster / TCM (telephonic case manager) and
  claimant, employer, providers
- **Quoted message content** — full or partial email/text bodies are embedded

---

## Cross-cutting parsing challenges

- **Date formats vary** — `MM/DD/YYYY`, `M-D-YY`, `12/21/24`, `Apr 17 2025`.
- **Encoding artifacts** — `â€"` / `â€”` for dashes, `â€™` for apostrophes.
- **Heavy restatement** — the same facts recur across many Resolution Strategy
  snapshots; an event model needs deduplication / "source of truth" resolution.
- **Future-dated content** — appointment dates within a note often post-date the
  note's own timestamp.
- **Style divergence between claims** — different adjusters use different layouts,
  abbreviations, and section headers; the parser cannot assume one fixed template.
- **Mixed structure** — some content is semi-structured (`Field: value` lines,
  Reserving entries) and some is pure prose (treatment summaries, emails).
- **Terminology / abbreviation density** — EE/IE/IW/clmt, OOW, LD, MMI, FCE, NOV,
  TTD/TPD/PPD, ACDF, HEP, etc. — needs a glossary / normalization layer.
- **Data quality anomalies** — at least one inconsistent date of injury appears.

---

## Next step

A separate document will cover **how to apply this inventory to answer
questions** — starting with the four sample queries (return-to-work duration,
appointments attended, reserve changes, time-to-provider) — including which of
the data points above each query depends on and how reliably each can be
extracted.
