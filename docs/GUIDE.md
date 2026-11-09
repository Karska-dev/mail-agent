# Mail Agent — user guide

Everything about using Mail Agent: the app, the terminal commands, sessions, auto mode, rules and how it works inside. For a quick overview, see the [README](../README.md). For privacy and data storage, see [SECURITY.md](../SECURITY.md).

## Contents

- [The app](#easiest-the-app-opens-in-your-browser) · [The Mac app](#the-mac-app)
- [Quick start (terminal)](#quick-start-terminal)
- [Using it on your real inbox](#using-it-on-your-real-inbox-gmail-or-any-imap-account)
- [Sessions and stopping early](#sessions-how-much-it-reads-and-stopping-early)
- [Auto mode](#auto-mode-the-whole-inbox-no-questions)
- [To-dos and notes](#to-dos-and-notes-written-by-the-ai)
- [Important tags and scam warnings](#important-tags-and-scam-warnings)
- ["Let me read it first" rules](#let-me-read-it-first-rules)
- [One-off emails](#one-off-emails-read--archive-no-rule)
- [Reviewing the original email](#reviewing-the-original-email)
- [How it works](#how-it-works)
- [Files](#files) · [Extending](#extending) · [Limitations](#limitations)

## Easiest: the app (opens in your browser)

**On a Mac:** double-click **Start Mail Agent.command** in the project folder. The first time, macOS may say it's from an unidentified developer. If so, right-click the file → **Open** → **Open**. It only asks once.

**Or from Terminal**, inside this folder:

```bash
python3 -m email_agent app      # or just `mail-agent` if you installed it with pipx
```

A page opens at `http://127.0.0.1:8765`. It runs only on your computer, and the Terminal window that opens has to stay open while you use it. From that page you can:

- **Try the demo** or **connect your Gmail**. Use an app password, and start with *Practice mode* switched on: the agent learns and shows what it would do, but nothing in Gmail is moved, deleted or marked as read.
- Handle one email at a time. Each email shows up as a card with what the agent found in it. Answer in your own words or use the quick-answer buttons: *Just read & archive (no rule)*, *View original* and *Skip*. When it shows a numbered menu, you can press the number keys.
- Press **Stop for now** at any moment. Whatever hasn't been handled stays unread for next time.
- Use the sidebar tabs: **To-dos** (tick them off; finished ones stay crossed out until you close the app, then they're tucked away behind "Show N finished from earlier"; untick to undo, or delete), **Notes**, **Rules** (see why each one exists, or forget one), **Read log** and **History**. From any of them you can open the original email and put it back in your inbox.

**Several mailboxes, one-time setup.** Add each address once with **+ add another mailbox**. The app checks the app password with the mail server and then saves it in your Mac's **Keychain**, the same secure store Safari and Mail use. You'll see it in the Keychain Access app under "Mail Agent". After that you just tick the mailboxes you want and press **Start reading**. They are read one after another in the same session, and your rules apply to all of them. **remove** deletes a mailbox and its saved password. Other IMAP providers such as Outlook, Yahoo and iCloud should work too (set the IMAP server under "Not Gmail?"), but so far only Gmail has been tested with real mail.

**Who reads your mail.** In the app you can choose between three readers:
- **Built-in reader.** Free and private. Simple pattern matching.
- **Local AI (Ollama).** Free and private. An open AI model runs on your own Mac, so emails never leave it. Install the free [Ollama](https://ollama.com/download) app and open it once. The app then offers a model sized for your Mac's memory (qwen3.5:9b, or qwen3.5:4b for 8 GB Macs) and downloads it with a progress bar. If Ollama isn't running when you start a session, the agent says so and uses the built-in reader for that session. The built-in reader also acts as a safety net: coupon codes, account numbers and dates the model misses are filled in from it.
- **Claude.** Paid. Needs an API key (see below). Best understanding.

**Speed with local AI.** A 7B model on a laptop can take a minute or more per email, so the agent works around that:
- It reads the *next* email in the background while you're answering the current one.
- Emails that match one of your sender rules are handled by the built-in reader without the AI.
- Long tracking links are shortened before the model sees them, and only the first ~3,500 characters are sent.
- Short answers such as "Archive it" are understood instantly.
- The model is loaded as soon as a session starts.

Each card shows how long it took ("read by local · qwen2.5:7b in 12s"). **Which model:** the app lists newer models marked **new** (checked against ollama.com in Sep 2026):
- **qwen3.5:9b** for 16 GB Macs, **qwen3.5:4b** for 8 GB Macs, and **gemma4:e4b**;
- the older **qwen2.5** models, which are proven with this app.

Newer models skip their slow "think out loud" step here, since it isn't needed for this job. **Check for updates** downloads a newer version of the model you're using, if Ollama has one. Try a new model in practice mode before relying on it.

**Smarter reading with Claude (optional).** Paste an Anthropic API key in the app and Claude reads the emails instead of the built-in reader. The key is checked, then saved in the Keychain. Nothing needs installing.

## The Mac app

**Mail Agent.app** (from the [releases page](https://github.com/Karska-dev/mail-agent/releases/latest), for Apple Silicon Macs) is the same app in a double-click package, with Python included.

- It has no window or Dock icon of its own: it starts in the background and opens its page in your browser.
- Opening it again while it runs just brings the page back; it never runs twice.
- Quit with the **Quit** button at the top of the page. It also quits by itself when no page has been open for 30 minutes and nothing is running.
- Messages that would normally go to the Terminal are written to `~/.email_agent/app.log`.
- It uses the same data (`~/.email_agent`) and Keychain entries as the Python version, so you can switch between them.
- First launch: the app isn't signed by Apple. On macOS 15 or newer, open **System Settings → Privacy & Security** and click **Open Anyway**; on macOS 14 or older, right-click the app and choose **Open**.

**Build it yourself:** double-click `packaging/mac/build-app.command` in the project folder. It sets up PyInstaller in a private folder (`.build-venv`) and puts **Mail Agent.app** in `dist/`. On GitHub, a release is made by **Actions → release → Run workflow**, or by pushing a version tag such as `v1.0.0`: it builds, checks and publishes the app for the version in `email_agent/__init__.py` (`.github/workflows/release.yml`).

## Quick start (terminal)

Requires Python 3.10 or later. The core has no dependencies.

```bash
python -m email_agent demo                 # scripted walkthrough of the whole story (12 emails)
python -m email_agent demo --interactive   # same inbox, but you answer the questions
```

To have Claude do the reading and understanding (recommended for real mail):

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or save it once in the app
python -m email_agent demo --interactive --engine claude
```

## Using it on your real inbox (Gmail or any IMAP account)

```bash
export EMAIL_AGENT_USER=you@gmail.com
export EMAIL_AGENT_PASSWORD=xxxx-xxxx-xxxx-xxxx   # Gmail: an App Password (Google Account → Security → App passwords)

python -m email_agent run --dry-run    # learn rules and see what it WOULD do; mail is not touched
                                       # (each session: newest 30 unread from the last 14 days)
python -m email_agent run              # for real: archive, trash, unsubscribe
python -m email_agent run --rules-only --watch 15   # unattended: apply known rules every 15 min, never ask
```

Other providers: pass `--host imap.fastmail.com` (or similar). On Gmail, *archive* removes the Inbox label and *delete* moves the message to Trash, so a delete can be undone for 30 days. The agent reads with `BODY.PEEK`, so it never marks mail as read.

```bash
python -m email_agent history [northwind]    # every email it handled, with a #number and where it is now
python -m email_agent show 12             # read original email #12 in full (--eml: path to the raw message)
python -m email_agent restore 12          # put #12 back in your inbox (from Archive or Trash)
python -m email_agent read                # digest of one-off "read & archive" emails
python -m email_agent rules               # what it has learned
python -m email_agent rules --delete r3   # make it forget one
python -m email_agent todos [--done 2]
python -m email_agent notes               # also written to ~/.email_agent/notes.md
python -m email_agent log                 # everything it did, and why
```

## Sessions: how much it reads, and stopping early

Each session reads **at most 30 unread emails** from **the last 14 days**, **newest first**. If new mail arrives while you're reviewing, the agent notices after each email and shows it next, marked **📨 just arrived**. The 30-email limit covers the whole session. When you read several mailboxes at once, it's split evenly between them (30 across 3 mailboxes = 10 each), and a mailbox with fewer new emails passes its spare slots on to the next one. Anything beyond it stays unread for the next session. (The demo is the exception: it reads its sample emails in date order so the story makes sense.) You can change both limits:

```bash
python -m email_agent run --days 7 --limit 10   # a quick pass over last week
python -m email_agent run --days 0              # no age limit
```

**Clearing out a whole inbox.** By default a session only looks at *unread* mail. To go through everything in the inbox, including emails you've already read, tick **Include emails I've already read** in the app, or use `run --include-read` in the terminal. Set **Look back** to 0 to include all dates. Work through it in batches of 30 until the app says there's nothing left. Emails you choose to keep stay in the inbox and aren't asked about again.

**Stopping early.** At any question, type `q` (or "stop", "finish", "done for now"), or press Ctrl+C. The agent stops right there. The email you were on and everything after it is left exactly as it was: still unread, still in the inbox, not moved. It all comes up again next session. Everything already handled stays handled, including the rules you taught it.

(The demo is the exception: it always starts over from its 12 sample emails.)

Shortcuts at the "What should I do?" prompt: `v` view the original · `r` read & archive, no rule · `s` skip this one · `q` stop for now.

## Auto mode: the whole inbox, no questions

Click **⚡ Auto mode — whole inbox** on the start screen. In the terminal, use `python3 -m email_agent run --auto`.

- It goes through **everything** in the inbox and ignores the look-back days and max-emails settings. It reads mail in small batches, so a big inbox doesn't have to download first. The two checkboxes still apply: *Include emails I've already read* and *Practice mode*.
- **It never asks you anything.** Emails your rules cover confidently are handled as usual.
- Emails that would need you are **skipped**. That covers new kinds of email, rules that might fit but the agent isn't sure, and suggested workflows. Skipped emails stay **unread in your inbox** and come up in your next normal session. The summary counts them as "need you".
- **"Read it first" rules** (like your security emails) save the email to **Notes** and **archive** it instead of deleting it. You can read it later from the note or from History. The rule itself doesn't change.
- Press **Stop** any time. Anything not handled yet stays in the inbox.
- With local AI, expect about 5–30 seconds per email the rules don't recognize instantly. A large inbox can take an hour or more.
- If the connection drops (Wi-Fi change, Mac waking from sleep), it reconnects and carries on.
- **Demo in auto mode** reuses the rules you taught in your last normal demo.

## To-dos and notes written by the AI

With an AI reader (local or Claude), to-dos and notes say what the email is really about, not the email's button text:
- a to-do reads like "Order class photos from Brightframe Studio by Sep 18", with a one-line summary underneath;
- a school digest becomes "Friday Folder — Maple Grove School";
- the **one link that matters** (Track package, Order photos, Pay bill…) is kept as a clickable **↗** link, so you don't have to open the email to find it. Links are never kept for emails flagged as possible scams;
- due dates are only shown when they're real dates.

Items created before this, or by the built-in reader, have a **✨ rewrite** link. It re-reads the original email with your AI reader and rewrites the to-do or note.

**Order:** to-dos and notes are listed **newest email first**, by the date of the email they came from. Each one shows "· email from Sep 28" so you can see the order. Finished to-dos stay at the bottom.

**Dates stand out:** a to-do with a date shows it on its own coloured line under the title, for example "📅 Thu, Oct 1 · 5pm · tomorrow". Red means overdue, orange today or tomorrow, amber this week and blue later. The date comes from the due date, or from a date in the title or summary. Due dates written as words ("Immediate") are shown as written.

**Clean up old notes & to-dos:** the To-dos and Notes tabs have a **🧹 Clean up old notes & to-dos** link at the top. It removes the ones about emails older than N days (30 by default). Age is the **email's** date, not when the note was made, which matters after a big inbox clean-up. It shows how many will go before you confirm. To-dos with a due date still ahead are kept unless you untick that option. Removed items are saved to `cleared/` in the data folder, and **undo it** brings the last clean-up back.

**Note → to-do:** every note has a **→ to-do** link. It creates a to-do with the note's title, its first real date as the due date, and the same ↗ link and original email. If you edited the note, your text comes along. The note stays and shows **✓ in to-dos**. Delete that to-do and you can convert the note again.

## Important tags and scam warnings

Every email card shows **★ Important** when it matters. That covers security alerts, bills and deadlines, appointments, messages from real people, and possible scams. Hover over the tag to see why.

When an email shows warning signs, a red **"Be careful — this could be a fake (phishing) email"** box lists them. The signs it looks for:
- a sender address that doesn't match the brand it claims, or a lookalike address such as chase-verify.net
- pressure wording ("account suspended", "within 24 hours")
- requests for passwords or card details
- shortened links, or links that go to a different site

Genuine security emails get a calmer blue note reminding you to check your account on the website itself. The AI readers (local or Claude) also judge importance and scam risk. The rule-based checks always run too, so an AI saying "looks fine" can't hide a concrete warning sign. No checker is perfect: treat the warning as a strong hint, and treat a missing warning as "no obvious signs", not as proof.

## "Let me read it first" rules

Some emails you want to see yourself, like security alerts. Answer with something like *"make me read it, then delete"* or *"show me security emails, then archive"*. The agent learns a rule with a **read-first** step. Every time that rule fires, it shows you the whole email and waits. After reading you can change your mind **for that one email**. The rule itself stays the same:
- **Done, as planned**: carries on with the rule (for example delete).
- **Archive / Delete it instead**, or **Keep it in my inbox**.
- **Something else**: type what you want, for example "add a to-do to check my Google account and archive it". Your words become the to-do title.
- **Skip for now**: you'll see it again next session.

Unattended runs (`--rules-only`) never skip this step. They leave these emails for you.

## One-off emails: read & archive, no rule

Some emails are one of a kind, like a tax letter or a one-time confirmation. You want to know what they said, but you don't want the agent to learn a rule from them. Whenever the agent asks what to do, you can answer:

- `r` (or "just read it and archive", "one-off", "no rule"). The agent writes a short digest entry to `read_log.md` with the summary and key facts, such as case or reference numbers. It then marks the email as read and archives it. **No rule is created**, and nothing is suggested from it later, so the next similar email is asked about again.
- `v` (or "show me the original"). This prints the full original email, and then the agent asks again.

Both options also appear at the bottom of every menu: when it suggests reusing another sender's workflow, and when it asks you to confirm a rule.

## Reviewing the original email

Originals open **formatted, like in a mail app**, with the sender's layout, colors and embedded pictures. A **Plain text** switch shows the text-only version. For safety, as regular mail apps do:
- nothing in the email can run code;
- links open in a new tab;
- **remote images are blocked** until you click **Load images**, because those images let senders see that you opened the email.


Before it moves anything, the agent saves a copy of every email it handles in `originals/` and gives it a number (`#12`). That number appears next to the email while the agent is working, and also in notes, to-dos, the read log and the activity log. So wherever you are looking, you can reach the original with `show 12`. For IMAP mail the raw `.eml` file is kept too, and it opens in any mail app. If you want the email back where it was, `restore 12` moves it back to your inbox on the server. On Gmail this also works for mail in Trash, for up to 30 days.

These copies live only in your data folder. Delete `originals/` whenever you like.

## How it works

```
email ──► Analyzer ──► Analysis ──► Matcher ──► auto / confirm / suggest / partial / none
          (Claude or     category        (rules.json)        │                     │
           offline)      features                            ▼                     ▼
                         extracted data                  Executor ◄── plan ◄── ask you (plain English)
                                                      notes · to-dos · archive      │
                                                      delete · unsubscribe          ▼
                                                                          learn rule (sender / any)
```

**Analysis.** Each email gets a *category* (promotion, bill, appointment, newsletter, …) and a set of *features*: `has_coupon`, `has_deadline`, `has_event`, `has_amount_due`, `has_reference`, `has_tracking`, `is_bulk`. The analyzer also pulls out *extracted data*: coupon codes and their conditions, offer expiry, due dates, amounts, account and order numbers, tracking numbers, appointment times and action items. Claude fills this in through a forced tool call. The offline engine produces the same structure with regex heuristics, and the agent falls back to it automatically if an API call fails.

**Rules.** A rule is made of conditions plus a workflow:

| part | example |
|---|---|
| scope | `sender` (only `northwind.example`) or `any` |
| category | `promotion` |
| required features | `has_coupon`, because the workflow saves a coupon code |
| excluded features | `has_deadline`, `has_amount_due`, … The rule was learned on an email without these, so an email that has one is treated as a different situation and the agent asks. |
| actions | `save_note(coupon_codes, conditions, expiry)` → `delete` |

**Matching:**

| outcome | when | what happens |
|---|---|---|
| **auto** | right sender (or an `any` rule), same category, all required features present, no excluded feature present, analysis confidence ≥ 0.6 | done silently and logged |
| **confirm** | an exact match exists, but confidence is low or two equally specific rules disagree | yes / pick one / tell me / skip |
| **suggest** | another sender's rule fits this email perfectly | reuse it once, for this sender, or for everyone |
| **partial** | a rule for this sender exists but doesn't fully apply (Northwind ad with no coupon) | explains why, then asks |
| **none** | nothing relevant | asks |

When more than one rule matches, a sender-specific rule wins over a general one, and a rule with more required features wins over one with fewer.

**Unsubscribing.** If an email offers RFC 8058 one-click unsubscribe, the agent does it directly. Otherwise it puts the unsubscribe link on your to-do list. If you have deleted the same sender's bulk mail three times through a rule, it offers to unsubscribe you.

## Files

```
email_agent/
  models.py        Email, Analysis, Rule + shared vocabulary (categories, features, fields)
  analyzer.py      offline analyzer (regex/keyword heuristics)
  llm.py           Claude analyzer + instruction parser (structured tool output, offline fallback)
  local_ai.py      local AI via Ollama (JSON-schema output + built-in reader as safety net)
  instructions.py  offline plain-English → actions parser
  rules.py         rule store, rule learning, matcher
  agent.py         the loop: analyze → match → act / ask → learn
  mailbox.py       LocalMailbox (demo), IMAPMailbox (Gmail-aware), unsubscribe
  stores.py        notes, to-dos, read log, originals archive (all json + markdown)
  ui.py            terminal UI + scripted UI
  webapp.py        local browser app (stdlib http.server, no installs)
  render.py        safe, formatted view of an original email (scripts removed, remote images blocked)
  cleanup.py       "clean up old notes & to-dos", and reading dates in to-dos
  cli.py           the `mail-agent` command
  keychain.py      saves app passwords / API key in the macOS Keychain
  netutil.py       secure connections that work with python.org's Mac installer
  web/index.html   the app's page
  demo.py          sample inbox and scripted answers
tests/test_agent.py
```

Data lives in `~/.email_agent/` (change it with `--data-dir` or `EMAIL_AGENT_DATA`). The demo writes to a temporary folder and starts fresh each time; your real data is never touched.

## Extending

- **Different destinations for notes and to-dos** (Todoist, Notion, Apple Reminders): swap `Notes` / `Todos` in `stores.py` for classes that have the same `add()` method.
- **New features or categories:** add them to `models.py`. The Claude schema picks them up automatically. The offline analyzer needs a regex for each one.
- **Rule edits:** `rules.json` is plain JSON that you can edit by hand.

## Limitations

- The offline engine is heuristic. It works well on typical retail, bill, appointment and newsletter mail, but on real mail Claude is noticeably better at unusual layouts and at deciding what is actually a coupon.
- By default a session looks at unread mail only; mail you've already read elsewhere is processed only with *Include emails I've already read* (`--include-read`).
- Emails that need your input stay in the inbox unread, so they come back on the next interactive run.
- Passwords go in the macOS Keychain; on other systems they're kept in a file only you can read (see [SECURITY.md](../SECURITY.md)). The app is built and tested on macOS.
