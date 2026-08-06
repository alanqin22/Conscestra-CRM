# Runbook — Personal data breach notification

**Read §1 before diagnosing anything.** The clocks start when you become aware,
not when you finish investigating.

**Privacy contact (named, as both regulators require):** Alan Qin —
privacy@agentorc.ca. This is the name that goes on the OPC report (s.10.1(3)(g))
and in the notification letter. Configured as `PRIVACY_CONTACT_NAME` and
`COMPLIANCE_CONTACT`.

**External counsel — call INQ Law, not Osler.** Two firms are retained and they
are not interchangeable:

| Firm | Scope | Call them |
|---|---|---|
| **INQ Law** | privacy, DSAR, breach response | **in an incident**, at §2 when the RROSH call is genuinely uncertain, and before any OPC report you are unsure of |
| Osler Startup Group | SaaS contracts, SLAs, certification | never during an incident — SOC 2 / ISO work and customer commitments |

Reaching for the wrong one costs hours you do not have. The RROSH assessment is
a legal judgement, not an engineering one, and the point at which you want
advice is the point at which the clock is already running — so call early, on a
maybe, rather than late on a certainty.

**Jurisdiction:** the controller operates from Ontario, Canada. **PIPEDA applies
to every breach.** GDPR applies additionally *only* if any affected individual is
in the EU/EEA. Most incidents here are PIPEDA-only, and PIPEDA is the one people
forget because the GDPR clock is the famous one.

---

## 1. The first ten minutes

**Write down the time you became aware.** Not the time of the breach — the time
you knew. Every deadline below runs from that moment, and you will not
reconstruct it accurately tomorrow.

Then answer one question: **could personal data have been accessed, lost,
altered, or disclosed to someone not entitled to it?**

If the answer is "possibly", you are in this runbook. Ambiguity counts.

| Obligation | Trigger | Deadline |
|---|---|---|
| **PIPEDA** — report to the Privacy Commissioner of Canada (OPC) | breach of security safeguards with a **real risk of significant harm** (RROSH) | "as soon as feasible" |
| **PIPEDA** — notify affected individuals | same RROSH test | "as soon as feasible" |
| **PIPEDA** — record the breach | **every breach, RROSH or not** | keep **24 months** |
| **GDPR Art. 33** — notify supervisory authority | any personal data breach unless unlikely to risk rights and freedoms | **72 hours from awareness** |
| **GDPR Art. 34** — notify data subjects | **high** risk to those individuals | without undue delay |

Two traps worth naming:

- **PIPEDA has no 72-hour number.** "As soon as feasible" is not more relaxed,
  it is less defined. Do not let the absence of a countdown imply slack.
- **PIPEDA requires a record of EVERY breach**, including ones you decide are
  not notifiable. The OPC can ask for those records. A decision not to notify is
  only defensible if it was written down at the time.

## 2. The RROSH test (PIPEDA)

"Real risk of significant harm" — significant harm includes humiliation, damage
to reputation or relationships, loss of employment or business opportunity,
financial loss, identity theft, and negative effects on a credit record.

Weigh two factors, per s.10.1(8):

1. **Sensitivity** of the information involved.
2. **Probability** it has been, is being, or will be misused.

Applied to this system's data:

| Data | Sensitivity | Notes |
|---|---|---|
| `auth_credentials` | **High** | password hashes; misuse probability high if exfiltrated |
| `contacts` / `leads` — name, email, phone | Moderate | identity-theft and phishing input |
| `payments`, `invoices` | Moderate-high | financial relationship; check whether card data is present (it should not be) |
| `customer_memories`, `conversation_messages` | **Context-dependent** | AI-held statements *about* people can be far more sensitive than the CRM fields — read them before deciding |
| `content_embeddings` | Moderate | derived from personal text; not human-readable, but not anonymous either |

**Do not treat embeddings as anonymous.** They are derived from personal text and
are re-identifiable in context. If the embedding store is in scope, say so.

## 3. Establish scope — what the system can tell you

Current record counts (2026-08-06; re-run, do not quote these):

| Table | Rows |
|---|---|
| `contacts` | 129 individuals (all with email and phone) |
| `leads` | 100 |
| `customers` | 38 |
| `accounts` | 131 organisations |
| `payments` / `invoices` | 1550 / 1409 |
| `customer_memories` | 304 AI-held statements about people |
| `content_embeddings` | 7774 embedded personal text records |

```powershell
# Everything held about one individual, for a subject who asks
python -m app.core.dsar --contact <uuid> --out subject.json

# The full list of tables holding a subject link — the scope boundary
python -m app.core.dsar --coverage
```

Who did what, and when:

```sql
SELECT * FROM audit_log       WHERE created_at > '<incident window>' ORDER BY created_at;
SELECT * FROM governed_deletions WHERE deleted_at > '<incident window>';
SELECT * FROM dsar_requests   ORDER BY created_at DESC LIMIT 20;   -- exports we made
SELECT * FROM memory_erasure_log ORDER BY erased_at DESC LIMIT 20;
```

If the breach involves **unauthorised database access**, check who was connected:

```sql
SELECT usename, client_addr, backend_start, state FROM pg_stat_activity;
```

The application connects as **`crm_app`**. A session as `postgres` from an
unexpected address is the shape of a compromise. `/health`'s
`database.connected_as` reports what the app itself is using.

## 4. Contain before you notify

1. Rotate the credentials implicated — [security_rotation_checklist.md](security_rotation_checklist.md).
2. If database credentials are implicated, rotate in Railway **and** confirm the
   app reconnects as `crm_app`, not `postgres`.
3. Revoke live sessions if the auth layer is implicated:
   `DELETE FROM auth_sessions;` (forces re-login; safe).
4. Take a dump **before** any remediation that changes data —
   `python -m scripts.backup_railway`. The pre-remediation state is evidence.

## 5. Report to the OPC (PIPEDA)

Form: **PIPEDA breach report**, opc-privacy.gc.ca → "Report a breach".
Required content (s.10.1(3) and the Breach of Security Safeguards Regulations):

```
1. Description of the circumstances and, if known, the cause.
2. Day or period during which the breach occurred (or best estimate).
3. Description of the personal information involved.
4. Number of individuals affected (or best estimate).
5. Steps taken to reduce risk of harm, or to mitigate it.
6. Steps taken or planned to notify affected individuals, and when.
7. Name and contact of a person who can answer questions on behalf of the
   organisation.  ->  **Alan Qin, privacy@agentorc.ca**
```

**Send an incomplete report on time rather than a complete one late.** The
regulations allow supplementing a report as more becomes known.

## 6. Notify affected individuals

Must be **direct** (email, phone, letter) unless direct notice would cause
further harm, is prohibitive, or you lack contact information — then indirect
(public notice) is permitted. All 129 contacts have email and phone on file, so
**direct notice is available and therefore expected.**

Notification must contain enough for the individual to reduce or mitigate harm:

```
Subject: Important notice about your information

On <date> we became aware that <plain description of what happened>.

WHAT INFORMATION WAS INVOLVED
<specific fields — "your name, email address and phone number", not
"certain personal information">

WHAT WE HAVE DONE
<containment, rotation, technical fixes — concrete and past-tense>

WHAT YOU CAN DO
<specific, actionable: change a password reused elsewhere, watch for phishing
referencing us, monitor statements. Only list steps that actually help for the
data involved.>

WHO TO CONTACT
Alan Qin, privacy@agentorc.ca

We reported this to the Office of the Privacy Commissioner of Canada on <date>.
```

Do not send this through the CRM's own outbound path if the CRM is implicated,
and never mark it `commercial=True` — a breach notice is not a commercial
electronic message and must not be suppressed by an unsubscribe list.

## 7. GDPR — only if an EU/EEA individual is affected

Check before assuming it doesn't apply:

```sql
SELECT DISTINCT country FROM addresses;         -- or the relevant location column
SELECT count(*) FROM contacts c JOIN accounts a USING (account_id)
 WHERE a.country IN ('DE','FR','IE','NL','ES','IT','PL','SE','BE','AT','DK','FI','PT','GR','CZ','RO','HU');
```

If yes: Art. 33 notification to the lead supervisory authority within **72
hours**, containing nature of the breach, categories and approximate numbers of
data subjects and records, DPO/contact point, likely consequences, and measures
taken. Art. 34 notice to the individuals themselves if the risk is **high**.

If the 72 hours will be missed, notify anyway **with reasons for the delay** —
that is explicitly provided for, and a late notification with an explanation is
far better than a silent one.

## 8. Record it — required even when you do not notify

PIPEDA s.10.3: keep a record of **every** breach of security safeguards for
**24 months**, whether or not it met the RROSH threshold.

**There is no breach register in this system today** (`breach_register` does not
exist). Until one does, record each incident in a durable, dated file and keep
it for two years. Minimum content:

```
Date/time of breach (or estimate)      Date/time became aware
What happened                          Cause, if known
Personal information involved          Number of individuals
RROSH assessment: notifiable? WHY / WHY NOT   <- the part people omit
Reported to OPC?  date / not required, because ...
Individuals notified? date / not required, because ...
Containment and remediation
```

The "why not" line is the one that matters. A decision not to notify is
defensible only if the reasoning existed at the time and was written down.

## 9. Gaps in this runbook

- **Never exercised.** No tabletop, no dry run. The first use will be the first
  test — and unlike the restore, this one cannot be rehearsed against a scratch
  copy.
- **No breach register table.** §8 is a manual process.
- **Named privacy contact: Alan Qin** (recorded 2026-08-06). No DPO is
  appointed; GDPR Art. 37 does not require one here, but if EU processing grows
  the question returns.
- **Counsel identified 2026-08-06: INQ Law** (privacy/DSAR/breach). Not yet
  briefed on this system, and there is no retainer or out-of-hours number on
  file — "we know who to call" is weaker than "we have called them once".
- **No cyber-insurance notification path.** Most policies require notice within
  a fixed window, and missing it can void cover.
