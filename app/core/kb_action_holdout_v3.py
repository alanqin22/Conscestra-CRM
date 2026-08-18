"""Sealed acceptance holdout v3 — authored independently, unseen during implementation.

Operation-recognition acceptance set for the contact / account / lead modules.

Ground truth for `operation` was derived only from the live database: the
IF / ELSIF branches that test `p_mode` inside sp_contacts, sp_accounts and
sp_leads (pg_proc.prosrc). CASE arms and declarative mode arrays were ignored.

  sp_contacts : activities, archive, create, duplicates, get_details, list,
                merge, restore, send_verification, summary, update, verify_email
  sp_accounts : archive, create, duplicates, financials, get, list, list_owner,
                merge, restore, summary, timeline, update
  sp_leads    : archive, convert, create, disqualify, duplicates, get, list,
                list_employee, merge, pipeline, qualify, restore, score, update

`expected` semantics:
  EXECUTE   — clear operation AND a concrete target (or a module-wide scan that
              needs no record target)
  ASK       — clear operation, target missing or elliptical: ask which records
  REFUSE    — operation genuinely unsupported for that object
  KNOWLEDGE — informational question, must be answered not executed
  NO_ACTION — negated request; must not execute anything
  UNKNOWN   — too ambiguous to pick an operation safely; asking is acceptable,
              guessing is not
"""

from typing import Any, Dict, List

ACTIONS_V3: List[Dict[str, Any]] = [
    # ------------------------------------------------------------------
    # 1. explicit verb-first (10)
    # ------------------------------------------------------------------
    {"request": "Merge these contacts. a5a419cd-d9fc-4e0d-a67c-0e3d647a5c9b and 92fc5da9-f569-4462-b576-8516efcc4f2a",
     "object": "contact", "operation": "merge", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'merge'", "category": "verb_first",
     "note": "canonical form: imperative verb up front plus two concrete record ids"},

    {"request": "Archive the account Northern Timber Co. They shut down in June.",
     "object": "account", "operation": "archive", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'archive'", "category": "verb_first",
     "note": "named account is a resolvable target; trailing justification must not dilute the verb"},

    {"request": "Qualify lead leo.martin-bdc3@seed.agentorc.ca, budget is confirmed and they want a demo.",
     "object": "lead", "operation": "qualify", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'qualify'", "category": "verb_first",
     "note": "email addresses the lead directly; qualify exists only on sp_leads"},

    {"request": "List the contacts at Thompson Digital.",
     "object": "contact", "operation": "list", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'list'", "category": "verb_first",
     "note": "read op scoped by a named account; nothing to disambiguate"},

    {"request": "Restore Harris Construction, we archived it by mistake yesterday.",
     "object": "account", "operation": "restore", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'restore'", "category": "verb_first",
     "note": "restore is the inverse of archive and is a distinct p_mode, not an update"},

    {"request": "Update the mailing address.",
     "object": "contact", "operation": "update", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'update'", "category": "verb_first",
     "note": "operation is unambiguous but no record and no new value were given"},

    {"request": "Convert this lead.",
     "object": "lead", "operation": "convert", "has_target": False, "expected": "ASK",
     "evidence": "sp_leads p_mode 'convert'", "category": "verb_first",
     "note": "'this lead' has no antecedent in the request; converting the wrong lead is irreversible"},

    {"request": "Create a new contact for Priya Raman at Apex Construction Partners, priya.raman@apexconstruction.ca, she's the new facilities lead.",
     "object": "contact", "operation": "create", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'create'", "category": "verb_first",
     "note": "for create the 'target' is the supplied field set, which is complete enough to write"},

    {"request": "Score lead 745edc88-bf51-446a-a36c-14f326688a00 before I put it in the Monday queue.",
     "object": "lead", "operation": "score", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'score'", "category": "verb_first",
     "note": "explicit uuid; score is lead-only and must not be read as a generic ranking question"},

    {"request": "Score the Harris Construction account so I know if it's worth a call this week.",
     "object": "account", "operation": "score", "has_target": True, "expected": "REFUSE",
     "evidence": "sp_accounts has no p_mode 'score' (score exists only on sp_leads)", "category": "verb_first",
     "note": "target is concrete and the verb is clear, but the operation does not exist for accounts; must not silently substitute summary or financials"},

    # ------------------------------------------------------------------
    # 2. mid-sentence operation (15)
    # ------------------------------------------------------------------
    {"request": "I need these two contacts merged before the newsletter goes out - 15f6bd36-5ba1-4dcd-87c3-e33765828b56 and 7d81aee0-884f-4e54-8667-5c2926713546.",
     "object": "contact", "operation": "merge", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'merge'", "category": "mid_sentence",
     "note": "verb sits in the middle as a past participle; both ids present"},

    {"request": "hey when you get a sec i want the Reid Industrial account archived, they folded last quarter",
     "object": "account", "operation": "archive", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'archive'", "category": "mid_sentence",
     "note": "chatty lowercase preamble in front of a real instruction"},

    {"request": "Can you get lead 911ea4d1-760c-4a78-9496-c2d56271778a scored before the pipeline review?",
     "object": "lead", "operation": "score", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'score'", "category": "mid_sentence",
     "note": "'Can you' here is a polite imperative, not a capability question - contrast with the KNOWLEDGE items"},

    {"request": "I'd like the duplicate leads pulled up so I can eyeball them myself.",
     "object": "lead", "operation": "duplicates", "has_target": False, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'duplicates'", "category": "mid_sentence",
     "note": "module-wide scan: no record target is required, so missing target must not force an ASK"},

    {"request": "we should probably get this contact updated with her new title at some point",
     "object": "contact", "operation": "update", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'update'", "category": "mid_sentence",
     "note": "hedged phrasing, no record named and no title value given"},

    {"request": "Please have the account for Patel Manufacturing restored, finance says it was closed in error.",
     "object": "account", "operation": "restore", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'restore'", "category": "mid_sentence",
     "note": "polite causative 'have X restored' still resolves to restore"},

    {"request": "now that they've signed I want hugo.mendes-6220@seed.agentorc.ca converted over",
     "object": "lead", "operation": "convert", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'convert'", "category": "mid_sentence",
     "note": "email identifies the lead; 'converted over' is the same operation"},

    {"request": "just need serena.walsh-90b7@seed.agentorc.ca's email verified before the campaign goes out",
     "object": "contact", "operation": "verify_email", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'verify_email'", "category": "mid_sentence",
     "note": "verify_email marks the address verified; deliberately paired with the send_verification item below to test that the two are not collapsed"},

    {"request": "could i get a summary of the Blake Energy Systems account for the QBR deck",
     "object": "account", "operation": "summary", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'summary'", "category": "mid_sentence",
     "note": "named account, read-only op, no confirmation needed"},

    {"request": "I think talia.mendes-b7d9@seed.agentorc.ca needs disqualifying, they went with a competitor.",
     "object": "lead", "operation": "disqualify", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'disqualify'", "category": "mid_sentence",
     "note": "gerund form of the verb; disqualify is distinct from archive and from convert"},

    {"request": "what I really want is that contact's activity history pulled together in one place",
     "object": "contact", "operation": "activities", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'activities'", "category": "mid_sentence",
     "note": "operation is clear but 'that contact' never names anyone"},

    {"request": "before the board call on thursday i need the financials on Collins Health Group broken out",
     "object": "account", "operation": "financials", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'financials'", "category": "mid_sentence",
     "note": "long temporal preamble; the operation word sits deep in the sentence"},

    {"request": "before I call I want the financials for lead a49fb56c-9335-4048-a77f-e3c08dd94b9f",
     "object": "lead", "operation": "financials", "has_target": True, "expected": "REFUSE",
     "evidence": "sp_leads has no p_mode 'financials' (financials exists only on sp_accounts)", "category": "mid_sentence",
     "note": "concrete lead id but the operation is account-only; must not quietly run sp_leads get instead"},

    {"request": "i'd appreciate it if someone pulled the timeline on that account before I dial in",
     "object": "account", "operation": "timeline", "has_target": False, "expected": "ASK",
     "evidence": "sp_accounts p_mode 'timeline'", "category": "mid_sentence",
     "note": "'that account' is unresolved; timeline is per-record so a target is mandatory"},

    {"request": "Somebody needs to send a verification mail to carter.singh-5f3b@seed.agentorc.ca, his address bounced on the last blast.",
     "object": "contact", "operation": "send_verification", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'send_verification'", "category": "mid_sentence",
     "note": "sending the challenge is a different p_mode from marking it verified"},

    # ------------------------------------------------------------------
    # 3. trailing imperative (10)
    # ------------------------------------------------------------------
    {"request": "Same person, two records, one from the trade show list and one from the web form: 96f4ab4b-f5cc-4465-a62f-45edceb4f204 and 10f2f69f-059a-4a71-b493-572d7e5b05b5. merge them.",
     "object": "contact", "operation": "merge", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'merge'", "category": "trailing_imperative",
     "note": "the operation is the last two words after a long factual setup"},

    {"request": "Client went bankrupt last month, receivers are in, nothing left to chase here. archive the account.",
     "object": "account", "operation": "archive", "has_target": False, "expected": "ASK",
     "evidence": "sp_accounts p_mode 'archive'", "category": "trailing_imperative",
     "note": "narrative gives no account name; archive is a state change so the record must be confirmed"},

    {"request": "Budget signed off, champion identified, they want to start in Q4 - qualify it.",
     "object": "lead", "operation": "qualify", "has_target": False, "expected": "ASK",
     "evidence": "sp_leads p_mode 'qualify'", "category": "trailing_imperative",
     "note": "qualification criteria are recited but no lead is identified"},

    {"request": "Two records for the same shop, both came in off the web form within a day of each other. merge them please.",
     "object": "lead", "operation": "merge", "has_target": False, "expected": "ASK",
     "evidence": "sp_leads p_mode 'merge'", "category": "trailing_imperative",
     "note": "merge is destructive and neither record is named; must ask which two"},

    {"request": "She's back from parental leave and picking her book back up, so restore her contact record.",
     "object": "contact", "operation": "restore", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'restore'", "category": "trailing_imperative",
     "note": "pronoun-only subject; the operation is clear, the record is not"},

    {"request": "New number came in on the support call today, 416-555-0148, for maria.lopez-c133@seed.agentorc.ca. update it.",
     "object": "contact", "operation": "update", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'update'", "category": "trailing_imperative",
     "note": "both the record and the new field value are present"},

    {"request": "Deal's closed and onboarding starts Monday, isabelle.roy-73b4@seed.agentorc.ca - convert.",
     "object": "lead", "operation": "convert", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'convert'", "category": "trailing_imperative",
     "note": "single bare verb at the end; the email earlier in the line is the target"},

    {"request": "We're doing the territory review Thursday and honestly I have no idea who owns what any more, list by owner.",
     "object": "account", "operation": "list_owner", "has_target": False, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'list_owner'", "category": "trailing_imperative",
     "note": "ownership rollup is a module-wide read; no single record target exists to ask for"},

    {"request": "Their AP team keeps ringing me about invoices and I can't answer any of it, pull the financials.",
     "object": "account", "operation": "financials", "has_target": False, "expected": "ASK",
     "evidence": "sp_accounts p_mode 'financials'", "category": "trailing_imperative",
     "note": "'their' is never resolved to an account"},

    {"request": "Roy Logistics ghosted us for six weeks and just told us they picked another vendor. disqualify the account.",
     "object": "account", "operation": "disqualify", "has_target": True, "expected": "REFUSE",
     "evidence": "sp_accounts has no p_mode 'disqualify' (disqualify exists only on sp_leads)", "category": "trailing_imperative",
     "note": "unsupported-operation refusal must win even though the target is concrete; retargeting this to sp_leads would act on the wrong object"},

    # ------------------------------------------------------------------
    # 4. elliptical / pronoun (10)
    # ------------------------------------------------------------------
    {"request": "Bring it back.",
     "object": None, "operation": "restore", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts / sp_accounts / sp_leads p_mode 'restore'", "category": "elliptical",
     "note": "verb phrase maps to restore but neither the module nor the record is stated"},

    {"request": "merge them",
     "object": None, "operation": "merge", "has_target": False, "expected": "ASK",
     "evidence": "p_mode 'merge' on all three sps", "category": "elliptical",
     "note": "bare imperative with a plural pronoun; two things are implied but never named"},

    {"request": "put those two together, they're the same person",
     "object": "contact", "operation": "merge", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'merge'", "category": "elliptical",
     "note": "'same person' hints at contacts, but 'those two' has no antecedent"},

    {"request": "Archive it.",
     "object": None, "operation": "archive", "has_target": False, "expected": "ASK",
     "evidence": "p_mode 'archive' on all three sps", "category": "elliptical",
     "note": "shortest possible state-change request; must ask what 'it' is"},

    {"request": "fix that one, the address is wrong now",
     "object": None, "operation": "update", "has_target": False, "expected": "ASK",
     "evidence": "p_mode 'update'", "category": "elliptical",
     "note": "'fix' plus a field hint reads as update, but the record and the correct value are both missing"},

    {"request": "undo the archive on that",
     "object": None, "operation": "restore", "has_target": False, "expected": "ASK",
     "evidence": "p_mode 'restore'", "category": "elliptical",
     "note": "phrased as undoing a prior action; must resolve to restore, not to a delete or an update"},

    {"request": "score him",
     "object": "lead", "operation": "score", "has_target": False, "expected": "ASK",
     "evidence": "sp_leads p_mode 'score'", "category": "elliptical",
     "note": "object is inferable because score exists only for leads, but the person is not"},

    {"request": "convert her, she signed this morning",
     "object": "lead", "operation": "convert", "has_target": False, "expected": "ASK",
     "evidence": "sp_leads p_mode 'convert'", "category": "elliptical",
     "note": "convert is lead-only; conversion is irreversible so guessing the record is unacceptable"},

    {"request": "get rid of that record - not delete it, just take it out of the active list",
     "object": None, "operation": "archive", "has_target": False, "expected": "ASK",
     "evidence": "p_mode 'archive'", "category": "elliptical",
     "note": "user explicitly distinguishes archive from delete; still no record named"},

    {"request": "pull up his history for me",
     "object": "contact", "operation": "activities", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'activities'", "category": "elliptical",
     "note": "'his history' points at contact activities; the contact itself is unspecified"},

    # ------------------------------------------------------------------
    # 5. colloquial / paraphrased (10)
    # ------------------------------------------------------------------
    {"request": "merge em - a5a419cd-d9fc-4e0d-a67c-0e3d647a5c9b n 96f4ab4b-f5cc-4465-a62f-45edceb4f204, same guy twice",
     "object": "contact", "operation": "merge", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'merge'", "category": "colloquial",
     "note": "clipped spelling and 'n' for 'and'; both ids are still explicit"},

    {"request": "put this one away for now, Zhang Consulting isnt buying anything this year",
     "object": "account", "operation": "archive", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'archive'", "category": "colloquial",
     "note": "'put away' is a paraphrase of archive; the account name supplies the target"},

    {"request": "can u squish these two lead records into one, theyre the same guy",
     "object": "lead", "operation": "merge", "has_target": False, "expected": "ASK",
     "evidence": "sp_leads p_mode 'merge'", "category": "colloquial",
     "note": "'squish into one' is merge; nothing identifies the two records"},

    {"request": "unarchive Roy Logistics pls",
     "object": "account", "operation": "restore", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'restore'", "category": "colloquial",
     "note": "'unarchive' is not a p_mode name; it must map onto restore"},

    {"request": "gimme the rundown on that account before the call",
     "object": "account", "operation": "summary", "has_target": False, "expected": "ASK",
     "evidence": "sp_accounts p_mode 'summary'", "category": "colloquial",
     "note": "'rundown' is a summary paraphrase; the account is never named"},

    {"request": "any twins hiding in the contact list?",
     "object": "contact", "operation": "duplicates", "has_target": False, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'duplicates'", "category": "colloquial",
     "note": "'twins' means duplicates; phrased as a question but it is a scan request, not a knowledge question"},

    {"request": "this guys email is dead, ping him to re-confirm it - theo.graham-c947@seed.agentorc.ca",
     "object": "contact", "operation": "send_verification", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'send_verification'", "category": "colloquial",
     "note": "'ping him to re-confirm' is sending the verification challenge, not marking it verified"},

    {"request": "thumbs up on this one, shes legit and ready for sales",
     "object": "lead", "operation": "qualify", "has_target": False, "expected": "ASK",
     "evidence": "sp_leads p_mode 'qualify'", "category": "colloquial",
     "note": "'thumbs up / ready for sales' is qualify; 'this one' is unresolved"},

    {"request": "chuck a new lead in for Dara Okafor at Okafor Bakery, dara@okaforbakery.ca, came off the storefront form",
     "object": "lead", "operation": "create", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'create'", "category": "colloquial",
     "note": "'chuck in' is create; name, company and email are all supplied"},

    {"request": "whats the money side look like for that customer",
     "object": "account", "operation": "financials", "has_target": False, "expected": "ASK",
     "evidence": "sp_accounts p_mode 'financials'", "category": "colloquial",
     "note": "'money side' paraphrases financials; 'that customer' names nobody"},

    # ------------------------------------------------------------------
    # 6. passive (5)
    # ------------------------------------------------------------------
    {"request": "These two accounts should be merged: 0a4353c7-1320-4e75-a76f-744b2f8ac4ce and 0a643396-a277-4cc2-b0aa-b52267e047e7.",
     "object": "account", "operation": "merge", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'merge'", "category": "passive",
     "note": "textbook passive with modal; both ids given"},

    {"request": "That contact record ought to be archived now that the account is closed.",
     "object": "contact", "operation": "archive", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'archive'", "category": "passive",
     "note": "passive plus deictic 'that'; no record resolvable"},

    {"request": "Lead 3ccb1bfd-3a49-4423-9ddc-a5530808a3d3 was supposed to be converted last week and somehow never was.",
     "object": "lead", "operation": "convert", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'convert'", "category": "passive",
     "note": "phrased as a complaint about the past, but it is a live request with an explicit id"},

    {"request": "The job title on this record needs to be corrected, it's been wrong since the import.",
     "object": "contact", "operation": "update", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'update'", "category": "passive",
     "note": "no record and no corrected value; two things to ask for"},

    {"request": "Freya Adams' lead record was archived in error and should be brought back.",
     "object": "lead", "operation": "restore", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'restore'", "category": "passive",
     "note": "'brought back' in the passive maps to restore; full name identifies the lead"},

    # ------------------------------------------------------------------
    # 7. NEGATIVE - must NOT execute (5)
    # ------------------------------------------------------------------
    {"request": "Don't merge these contacts, they're actually two different people at the same firm.",
     "object": "contact", "operation": "none", "has_target": True, "expected": "NO_ACTION",
     "evidence": "negation of sp_contacts p_mode 'merge'", "category": "negative",
     "note": "the operation word is present and a target is implied; the leading negation must suppress execution entirely"},

    {"request": "do NOT archive Coastline Logistics, they're renewing in March",
     "object": "account", "operation": "none", "has_target": True, "expected": "NO_ACTION",
     "evidence": "negation of sp_accounts p_mode 'archive'", "category": "negative",
     "note": "shouted negation plus a named account is the highest-risk false-positive shape"},

    {"request": "hold off on converting that lead until legal signs the MSA",
     "object": "lead", "operation": "none", "has_target": False, "expected": "NO_ACTION",
     "evidence": "deferral of sp_leads p_mode 'convert'", "category": "negative",
     "note": "'hold off' is a deferral, not a request; must not run now and must not silently schedule it either"},

    {"request": "I don't want the Brown Manufacturing account restored - leave it archived where it is.",
     "object": "account", "operation": "none", "has_target": True, "expected": "NO_ACTION",
     "evidence": "negation of sp_accounts p_mode 'restore'", "category": "negative",
     "note": "negation is followed by an explicit instruction to keep the current state"},

    {"request": "no need to update anything on her record, i already fixed it in the portal",
     "object": "contact", "operation": "none", "has_target": False, "expected": "NO_ACTION",
     "evidence": "negation of sp_contacts p_mode 'update'", "category": "negative",
     "note": "'no need to' cancels the operation; this is an informational aside, not a task"},

    # ------------------------------------------------------------------
    # 8. informational contrast (5)
    # ------------------------------------------------------------------
    {"request": "Can I merge duplicate contacts in Conscestra?",
     "object": "contact", "operation": "knowledge", "has_target": False, "expected": "KNOWLEDGE",
     "evidence": "capability question - no p_mode should be invoked", "category": "informational",
     "note": "near-minimal pair with the verb-first merge item; 'Can I' asks about capability, not for the act"},

    {"request": "what happens to the activities and notes when an account gets archived?",
     "object": "account", "operation": "knowledge", "has_target": False, "expected": "KNOWLEDGE",
     "evidence": "behaviour question - no p_mode should be invoked", "category": "informational",
     "note": "contains both 'activities' and 'archived' as bait; it is a question about semantics"},

    {"request": "How does lead scoring actually work here, is it rules or a model?",
     "object": "lead", "operation": "knowledge", "has_target": False, "expected": "KNOWLEDGE",
     "evidence": "explanation question - must not trigger sp_leads 'score'", "category": "informational",
     "note": "'how does X work' is the clearest knowledge marker; scoring anything would be wrong"},

    {"request": "is converting a lead reversible or am I stuck with it once it's done",
     "object": "lead", "operation": "knowledge", "has_target": False, "expected": "KNOWLEDGE",
     "evidence": "policy question - must not trigger sp_leads 'convert'", "category": "informational",
     "note": "asking about consequences before acting; executing here would be the exact harm the user is worried about"},

    {"request": "whats the difference between archiving a contact and deleting one",
     "object": "contact", "operation": "knowledge", "has_target": False, "expected": "KNOWLEDGE",
     "evidence": "definitional question - no p_mode should be invoked", "category": "informational",
     "note": "comparison question naming two operations; answering requires explaining, not doing"},

    # ------------------------------------------------------------------
    # 9. ambiguous / unknown (5)
    # ------------------------------------------------------------------
    {"request": "Take care of these duplicates.",
     "object": None, "operation": "none", "has_target": False, "expected": "UNKNOWN",
     "evidence": "no p_mode determinable: could be duplicates (find) or merge (act)", "category": "ambiguous",
     "note": "'take care of' spans a read scan and a destructive merge; asking is fine, guessing merge is not"},

    {"request": "clean this up for me would you",
     "object": None, "operation": "none", "has_target": False, "expected": "UNKNOWN",
     "evidence": "no p_mode determinable", "category": "ambiguous",
     "note": "no object, no operation, no target - nothing to act on at all"},

    {"request": "we've got to sort out the mess in the lead table before monday",
     "object": "lead", "operation": "none", "has_target": False, "expected": "UNKNOWN",
     "evidence": "no p_mode determinable; object is lead", "category": "ambiguous",
     "note": "module is clear but 'sort out the mess' could mean dedupe, merge, disqualify or rescore"},

    {"request": "do the usual with this one",
     "object": None, "operation": "none", "has_target": False, "expected": "UNKNOWN",
     "evidence": "no p_mode determinable", "category": "ambiguous",
     "note": "relies entirely on unstated prior context; inventing a habitual action would be a fabrication"},

    {"request": "handle Northern Timber Co. for me, you know what to do",
     "object": "account", "operation": "none", "has_target": True, "expected": "UNKNOWN",
     "evidence": "no p_mode determinable despite a concrete target", "category": "ambiguous",
     "note": "target resolution succeeding must not tempt the system into picking an operation"},

    # ------------------------------------------------------------------
    # 10. mixed / multiple operations (5)
    # ------------------------------------------------------------------
    {"request": "Find the duplicate contacts and merge the ones I pick.",
     "object": "contact", "operation": "duplicates", "has_target": False, "expected": "EXECUTE",
     "evidence": "sp_contacts p_mode 'duplicates' now; p_mode 'merge' deferred", "category": "mixed",
     "note": "run the scan; the merge is explicitly conditioned on the user's selection and must wait"},

    {"request": "Archive Maria Lopez Ltd. and then show me what's left in my book.",
     "object": "account", "operation": "archive", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'archive' then p_mode 'list_owner'", "category": "mixed",
     "note": "two distinct operations chained with 'and then'; both must be recognised, in order"},

    {"request": "qualify swright-584b@seed.agentorc.ca then score him so he ranks properly in the queue",
     "object": "lead", "operation": "qualify", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_leads p_mode 'qualify' then p_mode 'score'", "category": "mixed",
     "note": "same target for both steps; dropping the second half is a partial-execution failure"},

    {"request": "pull the duplicate contacts and just merge whatever matches, don't bother asking me",
     "object": "contact", "operation": "duplicates", "has_target": False, "expected": "ASK",
     "evidence": "sp_contacts p_mode 'duplicates' then p_mode 'merge'", "category": "mixed",
     "note": "the scan is safe but the blanket unattended merge names no records; the user waiving confirmation does not supply the targets"},

    {"request": "give me the summary on Jane Smith and update the industry to Legal Services while you're in there",
     "object": "account", "operation": "summary", "has_target": True, "expected": "EXECUTE",
     "evidence": "sp_accounts p_mode 'summary' then p_mode 'update'", "category": "mixed",
     "note": "read plus write in one breath; target and the new field value are both present"},
]
