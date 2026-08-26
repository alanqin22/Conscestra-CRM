# Conscestra CRM — Capabilities Guide

This guide covers six capabilities that customers and new users ask about most
often: working in your own language, bringing your existing records in, putting
the assistant on your own website, building your own agent, automated follow-up
sequences, and getting a price quote.

## Can I get help in my own language?
Yes. The Conscestra assistants understand and reply in English, French, Spanish
and Chinese, and they do it on every channel — the phone line, SMS, the website
chat and email. You do not need to select a language anywhere, dial a different
number, or open in English and then switch. Ask your question the way you would
ask a person, and the answer comes back in the language you used. If you change
language part-way through a conversation, the assistant follows you rather than
holding you to the language you opened with. On the written channels the same
rule applies to whatever arrives: an email or a text message written in Spanish
is answered in Spanish, without anyone having to route it anywhere special
first.

This holds even when the underlying knowledge was written in another language.
The knowledge base is searched across languages, so a question asked in French
or Chinese still finds the right English article and the assistant answers from
it in your language. That means you get the same answer, from the same source of
truth, whichever language you happen to be working in. There is no smaller
translated knowledge base running behind the main one, and no topics that are
only available to English speakers because a translation was never finished.

On a phone call both halves of the conversation move together: the assistant
listens in your language and speaks back in the same language and voice, rather
than understanding you in French and replying in English. Calls are the hardest
case for any assistant, so if it mishears a word — a company name, an order
number, an address — simply say it again or spell it out. The call carries on
from where it was; you are not sent back to the beginning, and nothing you have
already confirmed has to be repeated.

You can also ask to be put through to a person at any point, in any of the
supported languages, and that request is understood as an instruction rather
than treated as another question to answer. What you have already told the
assistant travels with you, so the colleague who picks up can see the
conversation so far instead of starting from a blank page and asking you to
explain the problem a second time.

## How do I bring my existing customers and contacts into Conscestra?
Conscestra imports your existing book of business from CSV files, so you do not
have to retype anything or begin from an empty system. Accounts, contacts and
leads can all be brought in this way, which covers the records most businesses
already keep in a spreadsheet or can export from another system. An export from
accounting software such as QuickBooks or Xero is usually a workable starting
point, and the Get Started page walks through the process step by step. You can
also bring your records in stages rather than all at once — accounts first and
contacts later, for instance — because each import is checked against what is
already there.

Every import runs in two stages: a preview, and then a commit. The preview reads
your file, matches its columns to the fields Conscestra expects, and shows you
exactly what would be created before anything at all is written. Nothing is
saved until you confirm. A mis-mapped column, an unexpected date format or a
stray header row is therefore something you see and correct on screen, rather
than something you discover afterwards scattered through your live data.

Imported records travel the same path as records typed in by hand. That matters
more than it may sound: the same validation rules, the same audit trail and the
same duplicate checks apply to a thousand rows as to one. A bulk import cannot
quietly introduce records that would have been rejected if someone had entered
them individually, and every imported record carries the same history as any
other, so you can always see where a given row came from.

Rows that match something already in the system are reported back to you as
duplicates instead of being added a second time. This makes an import safe to
repeat: if a handful of rows fail validation, you can fix just those rows in
your spreadsheet and run the file again without ending up with two copies of
everything that succeeded the first time. Imports are therefore something you
can iterate on rather than an operation you have to get perfect in one attempt.

## Can I put the Conscestra assistant on my own website?
Yes. The assistant is available as a drop-in chat widget that you add to any
website with a single line of HTML — one script tag carrying an embed key that
identifies your site. There is nothing to install, no build step, and no
framework requirement. It works equally well on a hand-written page, a site
built with a content management system, or a modern JavaScript application, and
a demonstration page is included so you can watch the widget running and try a
key out before you put it anywhere public. Adding it is a change to one line of
your page rather than a project: nobody needs to rebuild your site or learn how
the assistant works internally to put it live.

Embed keys are scoped to the web addresses you nominate. A key issued for your
domain only functions when the widget is loaded from that domain, so a key that
leaks cannot be used to run your assistant on somebody else's site. The key
itself is public by design — it ships inside your page's HTML where anyone can
read it — which is precisely why the origin restriction, rather than secrecy, is
what protects it. You can issue several keys, for instance one for your live
site and one for staging, and revoke any of them independently.

An embedded widget is deliberately limited in what it can see. It reads only the
public tier of the knowledge base — the same articles the phone, SMS and email
assistants may quote to a customer — and never the internal tier your staff rely
on. Marking an article internal is therefore sufficient to keep it off your
public website. There is no second switch to remember, and no configuration
mistake that lets a widget reach past that line into internal material.

A visitor's messages thread into a single ongoing conversation rather than
arriving as disconnected one-off questions. This is what makes handing over to a
person work properly: when a visitor asks for a human, a colleague can pick the
conversation up and read what has already been said, instead of receiving a bare
alert and having to ask the visitor to start again. The visitor experiences one
continuous conversation that happens to change who is answering.

## Can I build my own AI agent without writing code?
Yes. The Agent Studio lets a business administrator compose an entirely new
agent by describing it rather than programming it. You give the agent a display
name, a short description, plain-language instructions for how it should behave,
who is allowed to reach it, the knowledge tier it may read, and a few example
questions that show people what it is for. You do not need to know anything
about how the built-in agents are implemented in order to create one of your
own, and the agent you author is used the same way as the ones that ship with
the product.

Saving does not put the agent live. Your work is kept as a draft, so editing an
agent that is already serving customers never changes its behaviour as a side
effect of typing. The studio has a test box where you can talk to the draft and
see how it answers before anyone else can. To release it you run a safety
evaluation and then publish, and publishing stays unavailable until that
evaluation has run — an agent reaches real conversations only after it has been
checked and a person has deliberately released it. Every version is kept, so you
can see what changed between one release and the next.

Because the instructions are written in plain language, the people who actually
understand the process can author and refine the agent themselves. The service
manager who knows how returns really work, or the finance lead who knows which
invoice questions come up, can adjust an agent's wording directly instead of
filing a request and waiting for someone else to find time for it.

An authored agent can do more than answer questions: it can be granted specific
capabilities so that it acts on your behalf. Those grants are the entire
boundary of what it is able to do. An agent cannot invent a capability it was
never given, and it cannot widen its own permissions — the scope it was created
with is the scope it keeps until a person changes it deliberately.

Anything that would change your data becomes a governed proposal rather than an
immediate write. The proposal waits in the approval queue for a person to accept
or reject it, which means an agent authored by a non-technical colleague still
cannot make an unreviewed change to a customer record. Agents intended for
external use are additionally restricted to the public knowledge tier, so
publishing an agent to your website cannot expose internal material through it.

## What are sequences, and how do automated follow-ups work?
A sequence is a multi-step playbook that unfolds over days rather than all at
once. Most automation reacts instantly to a single event and then stops, which
cannot express the way follow-up actually works in practice: reach out, wait,
check whether anything came back, try a different approach, and eventually stop
trying. Sequences are how Conscestra expresses that shape — the waiting is part
of the design rather than something a person has to remember to do. Sequences
run on the same event bus the rest of the agents use, so a step can be triggered
by something that happens elsewhere in the business rather than only by the
clock.

The lead follow-up sequence is a good illustration. When a lead turns hot, the
sequence drafts an introduction email about two hours later, raises a reminder
task for the owner three days after that, offers to book a meeting a day later
again, and moves the lead into nurture after a week if nothing has come back.
Other sequences follow the same pattern for different situations, such as a save
attempt when an account starts showing signs of churning.

Every step re-checks the situation before it acts. A lead who replies or books a
meeting on day two does not then receive the day-three nudge, because the step
looks at the world as it is now rather than as it was when the sequence started.
Sequences can also branch: a lead who accepts a meeting moves onto a different
path from one who stays silent, and a sequence that has achieved what it set out
to do exits early instead of running to the end for the sake of it.

Sequences observe the same guardrails as everything else in the system. Outreach
is opt-in and off by default, emails are prepared as drafts for review rather
than sent silently, and anyone who has unsubscribed is excluded no matter what a
sequence would otherwise do next. You can see which sequences are running, which
step each one has reached, and why a particular step fired or was skipped — so
an automated follow-up is never something that happened for reasons nobody can
reconstruct afterwards.

## How do I get a price quote?
Ask for one in plain language. A request such as "quote Acme Corporation for
three label printers" is enough on its own. The assistant finds the account,
resolves its primary contact, matches each product you named against the
catalogue, and prices every line from the current retail price held in the
product pricing records. You do not need to look up product codes, copy prices
across from a price list, or know how the catalogue is organised internally. You
can ask from the AI bar inside the CRM or from the email assistant, whichever
you happen to be working in at the time.

The figures come from live pricing data rather than from anything the assistant
composes itself, and this is the most important thing to understand about how
quoting works here. The prices on the quote are the same prices the rest of the
system uses. A quote cannot drift away from your real pricing because an
assistant approximated a number, remembered an old figure from a previous
conversation, or produced something plausible-looking under pressure.

Where the request is ambiguous, the assistant says so instead of guessing. If a
product name matches nothing in the catalogue, or matches several things, you
are told which part could not be resolved rather than being handed a quote that
looks complete but quietly contains the wrong item. Being told that one line
could not be matched is far less costly than discovering it after a customer has
accepted the price.

The finished quote is prepared as an email to the contact, and like all outbound
communication it is governed. It is presented for review before it goes out, and
a discount beyond the permitted range requires approval rather than being
applied automatically. The intent is to make quoting fast without turning it
into a way to send a customer a price that nobody checked — the speed comes from
removing the lookup work, not from removing the review.
