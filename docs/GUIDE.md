# Detailed Guide: C2 Program Status Reporting Pipeline

n8n orchestration plus a FastAPI agent service for the fictional "Meridian Defense Systems C2 Platform Program". Every week, the pipeline pulls tasks and risks from Asana plus a milestone reference and engineering notes, runs each milestone through three LLM agents (Analyst, Executor, Slide title), drafts the weekly Slack post, and sends it to a human approver as a Slack DM before anything public happens. Only after that approval does the pipeline post to `#c2-program-status` and — via Workflow 3 — build a leadership deck with Claude that a second human approval releases to a distribution list. (The original template-based Workflow 2 is kept in `n8n/legacy/`.) See [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md) for the fuller narrative, the debugging lessons learned along the way, and an honest look at where the slide-deck automation does and doesn't hold up.

```
Workflow 1 (report):
  Asana (tasks, risks)  --\
  milestones.xlsx, notes  ---> Build request -> /clusters -> Analyst -> Executor -> Slide title
                                                                                        |
                              report_<date>.txt <- Format report (Read notes) <--------+--> report_<date>.json (sidecar, "Save sidecar")
                                     |
                              Gmail (report text only -- currently deactivated, kept as an option)

                              Format Slack message -> Request approval (Slack DM to the approver,
                                                          "Send and Wait for Response", Approval type)
                                                                    |
                                                          Approved? (IF: data.approved is true)
                                                    true /                              \ true
                                                        v                                v
                                          Send slack message                   Call 'Slide Build'
                                          (#c2-program-status,                  (Execute Workflow -> Workflow 3)
                                           new message weekly)
                                          false branch: unconnected -- a decline just ends the run, nothing posts or sends

Workflow 3 (deck review and distribute), triggered when Workflow 1's approval gate passes (or run by hand):
  Config -> read report_<date>.json + milestones.xlsx -> POST deck-service /render_weekly_deck
     |  (build failed -> Slack DM, nothing sent)
     v
  Read draft pptx + pdf -> Email draft to reviewer -> Slack "draft ready" DM
     -> Request deck approval (Gmail Send and Wait: approve / decline link, 72 h)
     -> Approved? --no--> Slack "you will send" (reviewer edits and sends the draft themselves)
           | yes
           v
        Distribution enabled? --no--> Slack "test mode" (nothing sent)
           | yes (flag true AND address list not empty)
           v
        Read the SAME saved pptx + pdf by path -> Send to distribution (Gmail) -> Slack "sent"

(Legacy) Workflow 2, the original template path: report_<date>.json -> Build slide bullets -> Copy Slides
template -> Replace Text -> Export to PDF -> Gmail. See "Known limitations" for why it was replaced.

agent-service (FastAPI):  n8n --HTTP (X-Service-Token)--> agent-service --Anthropic API--> Claude
                           POST /agents/<name>/run

deck-service (FastAPI):   n8n --HTTP (X-Service-Token)--> deck-service --Anthropic API (code execution + pptx skill)--> Claude
                           POST /render_weekly_deck   (renders the PDF itself, runs 21 checks, repairs up to 2 rounds)
```

## Setup

Requires Docker Desktop and Python 3. Commands are shown for Windows PowerShell; on macOS or Linux use the equivalent `cp` and forward-slash paths.

1. Create the secrets file and fill it in:

   ```powershell
   Copy-Item .env.example .env
   # Generate a random value (works in Windows PowerShell 5.1 and 7). Run it twice: once for
   # SERVICE_SHARED_SECRET and once for N8N_ENCRYPTION_KEY.
   $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); ($b | ForEach-Object { $_.ToString('x2') }) -join ''
   notepad .env    # set SERVICE_SHARED_SECRET, N8N_ENCRYPTION_KEY and ANTHROPIC_API_KEY
   ```

   Keep `N8N_ENCRYPTION_KEY` safe and never change it afterwards: n8n uses it to encrypt the credentials it stores, and a different key can no longer read them.

2. Copy the sample source data into the shared folder, then start n8n, the agent service and the deck service from the repository folder (`files\` is mounted into the containers as `/files`; `sample_data\` is the single committed copy of the data):

   ```powershell
   Copy-Item sample_data\* files\
   docker compose up -d --build
   docker compose logs -f agent-service
   ```

   n8n is then at http://localhost:5678, and the agent service is at http://localhost:8000 (bound to localhost only). Never run `docker compose down -v`: it deletes the n8n data volume, including your workflows and credentials.

3. Import the workflows from `n8n/`: `workflow-1-project-status-reporting.json` and `workflow-3-deck-review.json` (the original Workflow 2 is in `n8n/legacy/`). In Workflow 1, point the `Call 'Slide Build'` node at the imported Workflow 3. Credentials and identifiers were removed from the exports: reconnect your own credentials and replace the `YOUR_...` placeholders (email, Slack user ID, Asana project IDs, and the Header Auth credential for the two services). They are also described under *n8n workflows* and *n8n wiring notes* below and pictured in `docs/screenshots/`, and the Code-node scripts are in `n8n/`. Inputs for the file-reading nodes are in `files/`.

4. Smoke test (Python 3, no packages needed):

   ```powershell
   cd <repository folder>
   python scripts\smoke_test.py --no-llm    # no API spend
   python scripts\smoke_test.py             # also runs the Executor once
   ```

   In PowerShell, `curl` is an alias for `Invoke-WebRequest`, so use the script above or `curl.exe`.

5. First live run on the sample data (this spends a few API calls):

   ```powershell
   pip install openpyxl
   python scripts\run_analysis.py --dry-run     # inspect the clusters first, no API calls
   python scripts\run_analysis.py --milestone CDR --milestone ATO
   python scripts\run_analysis.py --out results.json    # every milestone with linked rows
   ```

   `sample_data\` holds the four source files as provided (milestones.xlsx, task and risk CSVs, program_notes.txt); use `--data-dir` to point at another folder. Each cluster goes to the Analyst, then the Analyst's output goes to the Executor. Expected known answers: ATO not flagged but high risk, and CDR flagged (radar adapter overdue, RISK-009 stale) with RISK-021 recommended for removal. The Customer Demo milestone has no known answer yet.

   The script adds `as_of_date` (today) to each cluster so the model doesn't have to guess the date when judging overdue tasks. Use `--as-of 2026-09-14` to pin it to the notes' week, or `--no-as-of` to send exactly what the original harness did.

Notes on the Windows folder mount:
- Files in `files` appear inside the n8n container at `/files`. Use the Read/Write Files from Disk node with a Schedule Trigger, as designed. Avoid the Local File Trigger node on this mount, because Windows file-change events may not reach the container.
- Bind mounts from Windows go through Docker Desktop and are slower than the Linux filesystem, which does not matter for a handful of small files.
- The `agents` folder is mounted the same way: edit instruction files on Windows, then `POST /admin/reload`.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness for Docker; no program data |
| GET | `/agents` | token | Lists agents and whether each one's instructions loaded |
| POST | `/agents/{name}/run` | token | Body `{"input": <string or JSON>}`. Returns `{output, model, usage, attempts, latency_ms, request_id}` |
| POST | `/admin/reload` | token | Re-reads instruction files (no rebuild needed) |
| POST | `/clusters` | token | Body `{milestones, tasks, risks, engineering_notes, as_of_date, include_empty?}`, rows keyed like the CSV/xlsx exports. Returns `{clusters, skipped, warnings}`: one cluster per milestone, ready to send to the Analyst. No LLM call. |

`output` is a validated object for JSON agents (the Analyst) and a string for text agents (the Executor). The Analyst's output is validated against `MilestoneRiskAnalystOutput` in `app/schemas.py`. If the model returns malformed JSON, the service retries once with the validation error fed back, then returns 502.

Status codes: 401 bad or missing token, 404 unknown agent, 413 input too large, 422 empty input, 502 or 504 LLM/provider failure, 503 instructions missing or API key not configured.

## n8n workflows

**Workflow 1 (report generation)** — `Get tasks`/`Map tasks` and `Get risks`/`Map risks` pull from Asana (native node or raw HTTP, `map_asana_tasks.js`/`map_asana_risks.js` normalize either shape to the CSV-import column names the Analyst was tuned on); `Read/Write Files from Disk`/`Extract notes` and `read milestones`/`Extract milestones` read `program_notes.txt` and `milestones.xlsx`. `Merge` (append) combines all four sources into one item, `Build request` (`build_request.js`) shapes it into the `/clusters` request body, and `clusters` + `Split Out` turn that into one item per milestone. Each item runs `Analyst` → `Executor` → `Slide title` (three separate `/agents/<name>/run` calls). `Read notes` (`format_report.js` — the name is a leftover and worth renaming) assembles the narrative report and a JSON sidecar, and fans out to `Report to text file`/`Save report` (writes `report_<date>.txt`), `Convert sidecar`/`Save sidecar` (writes `report_<date>.json`, the handoff file Workflow 2 reads), and `Format Slack message` (`format_slack_message.js`, reading the same item — no extra file read). Workflow 1's own Gmail node (report text only) is currently **deactivated, not deleted** — see "Known limitations" below.

Nothing public happens automatically from there. `Format Slack message` feeds `Request approval`, a Slack node (Resource: Message, Action: "Send a message and wait for response", Response Type: Approval) that DMs the drafted post to the approver and pauses the whole execution until they click Approve or Decline. Its output lands on `Approved?`, a plain IF node (Boolean condition `{{ $json.data.approved }}` is `true`). Only the IF node's **true** branch is wired up, to two parallel nodes: `Send slack message` (posts `{{ $json.slack_text }}` to `#c2-program-status` as a new top-level message — see the wiring note below on why it doesn't thread off the earlier test post) and `Call 'Slide Build'` (an `Execute Workflow` node pointed at Workflow 2). The **false** branch is left unconnected on purpose — a decline just ends the execution there, with no Slack post and no slide deck. `Save sidecar` no longer connects directly to Workflow 2; the sidecar file still gets written every run (so it exists on disk for later reference), but Workflow 2 only actually *runs* when the approval gate passes.

**Workflow 2 (slide deck) — legacy, replaced by Workflow 3 and kept in `n8n/legacy/`.** It starts from a `When Executed by Another Workflow` trigger (required for Workflow 1 to be able to call it; a Manual Trigger only responds to a click in its own editor tab, not to another workflow's `Execute Workflow` node), and now only ever fires from `Call 'Slide Build'` after a human has approved the week's report. `Read report json`/`Parse report json` load the sidecar, `Build slide bullets` (`build_slide_bullets.js`) splits each milestone's already-human-reviewed Executor paragraph into up to 5 bullets and flattens everything — `DATE`, `<KEY>_TITLE`/`_SUBTITLE`/`_B1..B5`, plus `report`/`subject` passed through from the sidecar — onto one item. `Copy file` (Drive, Copy) duplicates the Slides template; `Replace txt` (Google Slides, Replace Text) fills in the placeholders; `download slides pdf` (Drive, Download, with Google File Conversion set to PDF) exports it; `Send a message` (Gmail) sends the combined email — report text in the body, deck attached.

## Workflow 3 and the deck service

**Why it exists.** Workflow 2 filled a fixed Slides template, which cannot grow or shrink with the week's content (see "Known limitations"). Workflow 3 asks Claude to build the deck each week instead, from the vetted sidecar (`report_<date>.json`) plus `milestones.xlsx`, and treats the result as a draft for a person to finish. Everything the deck says comes from content a reviewer already approved: the deck service withholds the Analyst's own status and explanation notes and uses only the approved report text.

**The deck service** (`deck_service/`, FastAPI, port 8010, internal to the compose network). `POST /render_weekly_deck` takes `{sidecar, milestone_rows, program?, options?}` exactly as n8n's Extract From File nodes produce them, so n8n needs no Code node. The adapter joins the two by exact milestone name (a mismatch or duplicate is a 422, not a silent gap). The builder calls Claude with the code execution tool and the PowerPoint skill, using `deck_service/agents/deck_builder.md` as the instructions. The service then renders the PDF with LibreOffice and runs 21 deterministic checks (text overlap, shapes inside the slide, nothing in the footer band, accuracy flags matching the input, no invented ratings, and so on). Failed checks go back to the model with page images for up to `DECK_MAX_REPAIRS` rounds. A deck is always returned if one was produced; `status: "needs_review"` means checks still fail. Auth is the same `X-Service-Token` pattern as the agent service, failing closed. Error messages carry the error type only, and logs carry sizes and token counts, never content. Full request, response and configuration detail is in [`deck_service/README.md`](../deck_service/README.md).

**The workflow** (`n8n/workflow-3-deck-review.json`, node by node in [`WORKFLOW_3_SPEC.md`](WORKFLOW_3_SPEC.md)). A `Config` node holds `date`, `reviewer_email`, `slack_approver_id`, `distro_emails` and `distro_send_enabled`. The build node has a 20-minute timeout and retries off, since a retry would pay for a second run. The deck is generated once: release sends the saved file by path, so nothing regenerates between draft and send. Distribution needs the flag **and** a non-empty list, and both default to off.

**Which approval path.** The Slack "Send and Wait" buttons are interactive: Slack posts the click to a public HTTPS URL, so a localhost n8n never receives it (the buttons render, clicks do nothing). Gmail Send and Wait sends links that open in your own browser, which works locally. The main workflow therefore uses Slack for notices and Gmail for the approval click. `n8n/alternatives/` has the Slack-button version (for an n8n with a public URL, `WEBHOOK_URL` set, and the Slack app's Interactivity Request URL and signing secret configured) and a Gmail-only version. Workflow 1's own Slack approval has the same local-n8n limit.

**Cost control.** The first unconstrained run took 51 sandbox steps and cost $46.44, because each step re-sent a context that had grown from about 60k to over 500k tokens. The fixes: a work budget in the instructions (write the script once, run it once, no self-inspection), quality checks moved from the model into the service's own code, a hard cap of 6 model calls per request, no pause continuations, and a 20,000-token output limit (the SDK refuses more without streaming). A typical run is now one model call (about 130k input tokens, 2 minutes) or two when a repair round is needed (about 380k, 4 minutes), roughly $1 to $2. The service logs per-call token counts and the names of any failed checks so changes can be measured.

**Running it.** Set the new variables in `.env.example`, then `docker compose up -d --build deck-service` from the repo root. Build the Header Auth credential in n8n with the same `SERVICE_SHARED_SECRET`. To test without Asana or Workflow 1, import `workflow-3-deck-review.json`, set `Config.date` to a date that has a `report_<date>.json` in `files/`, and use the Manual run trigger. To test the send path safely, set `distro_send_enabled` true with only your own address in `distro_emails`.

## n8n wiring notes

- Create an n8n credential of type Header Auth with name `X-Service-Token` and your secret as the value, and attach it to each HTTP Request node. The secret is deliberately not passed to the n8n container as an env var.
- HTTP Request node: POST `http://agent-service:8000/agents/milestone_risk_analyst/run`, JSON body `{"input": {{ $json.cluster }}}`.
- Chain the Executor by sending the Analyst's `output` plus the milestone name as its `input`.
- Build clusters with POST `/clusters` (send `as_of_date` as `{{ $today.toISODate() }}`), then a Split Out node on `clusters` gives one item per milestone to send to the Analyst.
- Set the node timeout to at least 90 s (the service's own LLM timeout is 60 s, with SDK retries).
- If the Read/Write Files node reports "access not allowed" or "not writable", check that `N8N_RESTRICT_FILE_ACCESS_TO=/files` is set and honored by your n8n version. Writes to a Windows bind mount can also fail on permissions; if reads work but writes don't, say so and we'll look at it.
- **Code node edits don't sync from disk.** The `.js` files under `n8n/` are the source of truth for review, but each n8n Code node stores its script as text pasted directly into the node — editing the file here (or in this repo) does nothing to the live workflow until you re-paste it into the node. After changing one of these files, go update the corresponding node.
- **Expression fields: don't type the leading `=` yourself.** Once you toggle a field's `fx` icon on, everything you type is already the expression — write `{{ $json.foo }}`, not `={{ $json.foo }}`. Typing the `=` yourself produces a literal `=` character prepended to the evaluated result, silently, with no error. This bit us on a Drive file ID (`File not found: =1abc...`) and on every Slides placeholder value (`=ATO Approval Slipping...`) in the same session — easy to miss since the value still *looks* mostly right.
- **`$('Node Name')` needs an exact name match and a real connected path.** It resolves by the node's literal title string (rename a node and update every reference to it, e.g. we had `Copy a file` in an expression after the node itself was named `Copy file`) and only works if there's an actual wired connection back to that node — nodes sitting next to each other on the canvas isn't enough; n8n's own validator ("No path back to referenced node") catches the second case but not the first.
- **Resource-locator fields' "From List" mode can't see a file this same run is about to create.** It only queries what already exists in Drive right now. Point at a file created earlier in the same execution (e.g. the Slides copy) by switching that field to "By ID" and referencing the ID by expression instead.
- **A `Merge` node only combines branches inside one workflow.** Two separately-triggered workflows can't be merged directly; either bring the nodes into one canvas, or decouple them properly (we had Workflow 2 read the fields it needed off the same JSON sidecar file Workflow 1 already writes, rather than merging or cross-calling for data).
- **Pinned test data replays silently.** A pinned node keeps returning its frozen output regardless of later edits elsewhere in the workflow, including re-runs — which can make a real fix look like it didn't work (we chased this exact symptom on `Copy file` reusing an already-modified Slides file).
- **Slack node: post a new message each week, don't thread it.** `#c2-program-status` already has one manually-posted message in it (a test post from 2026-09-18, showing the report for the week of 2026-09-14) used as the reference for what `format_slack_message.js` should produce. The weekly automated posts are deliberately separate top-level messages in the same channel, not thread replies under that post or under each other — threading would bury each week's update behind "N replies" in the channel view instead of showing it directly. The `Send slack message` node sets Channel to `#c2-program-status` and Message Text to `{{ $json.slack_text }}`, and leaves `thread_ts` unset.
- **The Slack app needs its own workspace, not just a channel.** A bot app can only be installed into a workspace you actually administer. A channel you were invited into inside someone else's Slack Connect org (shows in the sidebar looking like an ordinary channel) is not enough — you can't install a custom app there, and a personal account isn't a workspace either. If you don't already own a workspace, create a new one (the browser flow supports this; the desktop app's "create workspace" option may not), add a channel, and build the app inside that workspace. From the app's **OAuth & Permissions** page, add Bot Token Scopes `chat:write`, `chat:write.public`, `channels:read`, and `users:read` (the last two are what populate the Channel/User dropdowns in n8n's Slack node — without them the dropdowns are just empty, which looks like a connection problem but isn't), then install the app and copy the **Bot User OAuth Token** (`xoxb-...`) into an n8n credential of type "Slack API" (Access Token). Invite the bot to `#c2-program-status` so it's allowed to post there.
- **The approval gate is n8n's Slack "Send and Wait for Response" action, not a separate approval node.** It's easy to miss because it isn't its own node type in the palette — add a Slack node, set Resource to Message, then pick Action **"Send a message and wait for response"**, and set Response Type to **Approval**. Point "User" (not "Channel") at the actual human approver — sending it to the bot itself, or leaving the default recipient, means the DM never reaches a person who can click anything. The node pauses that execution indefinitely (n8n's default wait limit is 48 hours) until the button is clicked, and its output includes `data.approved` (boolean) plus whatever comment the approver typed.
- **Gate both the public post and the slide deck on one IF node, not two separate checks.** `Approved?` reads `{{ $json.data.approved }}` from `Request approval`'s output; switch the field's type icon from the default String ("T") to Boolean, which turns "value2" into a true/false dropdown, and leave the operator as "is equal to" with `true`. Wire **both** `Send slack message` and `Call 'Slide Build'` off the **true** output, and leave the **false** output disconnected. If you're changing this wiring, double-check what `Save sidecar` (or whatever node used to trigger Workflow 2 directly) still connects to — an old unconditional connection left in place alongside the new gated one will fire Workflow 2 twice, once always and once only on approval.
- **An execution's canvas is a frozen snapshot; only the Editor tab is the live, saved workflow.** Opening a past execution shows exactly what ran *at that time*, including nodes that have since been deleted from the workflow — we lost the `Call 'Slide Build'` node this way (it showed up fine in old execution snapshots while being completely absent from the live Editor canvas after some canvas edits), which meant every subsequent approved run would silently skip the slide deck with no error. If something that used to work stops happening, check the Editor tab's actual current node list, not an old execution's canvas.
- **A stuck-looking "Waiting for response" node is usually a stale UI, or a second execution you forgot about.** Triggering the workflow again before resolving a previous run's approval DM doesn't cancel that run — it starts an independent execution with its own DM and its own Approve button, so you can end up with several stacked "Waiting" executions and confusing, interleaved signals (e.g. a Slack message going out from one execution while another still shows "waiting"). Use the Executions list's "Stop all" to clear everything before a clean test. Separately, if a node looks frozen (spinning, can't edit its config) but a fresh browser tab shows it's actually idle and editable, it's stale front-end state in that tab — a hard refresh fixes it; restarting the n8n container is only needed if the backend itself is genuinely stuck.
- **Don't hardcode a prefix in front of a field that already has one.** Workflow 2's Gmail node built its Subject as `Weekly Status Report{{ $('Build slide bullets').item.json.subject }}`, but the sidecar's own `subject` field (from `format_report.js`) already starts with "Weekly Status Report..." — so every email went out titled `Weekly Status ReportMeridian Defense Systems C2 Platform Program: Weekly Status Report (2026-09-24)`, duplicated and run together with no space. The node "succeeded" and the email arrived; the only sign anything was wrong was actually reading the subject line. Fixed by setting Subject to just `{{ $('Build slide bullets').item.json.subject }}`.
- **A successful send doesn't mean prompt delivery.** Both n8n's execution log and the Gmail API's own response can report success (a real message ID, `SENT`/`INBOX` labels) well before the email is visible anywhere — we saw a combined report+deck email sit for over an hour between a successful send and actually showing up, matching a plain Gmail-to-Gmail test email sent around the same time that was also delayed by several minutes. Don't treat "no email yet" a few minutes after a run as proof the pipeline failed; check the execution log and the Gmail node's own output first.

- **Workflow 3 gotchas (found while testing it).**
  - *Dates:* in an expression, an unquoted `{{ 2026-09-24 }}` is arithmetic and evaluates to 1993. For a test date, use Fixed text `2026-09-24`, or quote it in an expression. `Config.date` defaults to `{{ $json.date || $today.toISODate() }}` and must be switched back after testing.
  - *File not found:* the Read node reads from the container's `/files` mount. If the running container was started from a different folder than the one you are editing, it reads the old folder. Run `docker compose up -d` from the folder that holds the compose file you mean.
  - *`ENOTFOUND deck-service`:* the service is not defined in (or not started by) the compose file n8n came up with. `docker compose up -d --build deck-service` starts only that service, so run a plain `docker compose up -d` afterward if n8n is not on port 5678.
  - *Slack buttons do nothing:* see "Which approval path" above. A node that is waiting for a response cannot be stopped from the canvas; clear stale waiting executions from the Executions tab.
  - *Slack messages seem to vanish:* check you are signed in to the workspace the app was installed in before concluding the bot was removed.
  - *Gmail Send and Wait:* use `<br>` for line breaks in its message field.
  - *Asana custom fields are a paid feature.* After a trial ends, tasks come back without them and `Map tasks` stops with "No task has a Related Milestone value".

## Security and logging

- Shared-secret header, compared in constant time. The service refuses to start if `SERVICE_SHARED_SECRET` is empty.
- The host port mapping is bound to `127.0.0.1` only; n8n reaches the service over the internal compose network. Delete the `ports:` entry under `agent-service` if you don't need curl access.
- Logs go to stdout (`docker compose logs agent-service`) with a request ID on every line, and the same ID is returned in the `X-Request-ID` response header. INFO logs record sizes and token counts, never prompt or response content. Set `LOG_LEVEL=DEBUG` to log content while debugging, and don't leave it on.
- `.env` is git-ignored. Keep real program data out of git.

## Tests

```bash
cd agent_service
pip install -r requirements-dev.txt
pytest
```

The deck service has its own suite (39 tests): `cd deck_service && pip install -r requirements.txt pytest && pytest`. It also checks that the committed workflow files contain no credentials, Slack IDs or email addresses.

The tests use a fake LLM plus a mocked HTTP transport for the real SDK wrapper. They make no network calls and need no API key.

## Analyst instructions

- The Analyst's instructions are `agent_service/agents/milestone_risk_analyst.md`. They were cleaned up after the first live run: the milestone status line is always reported, the yes/no flag says only whether the reported milestone status is accurate, and low/medium/high risk ratings are left to the risk register.
- The service sends the Analyst `"Source data:\n" + <JSON>`, as `test_milestone_risk_analyst.py` does. From n8n, send the cluster object as `input`: `{milestone_reference, task_tracker, risk_register, engineering_notes}`, plus `as_of_date`.
- The default model is `claude-sonnet-5`, set with `ANTHROPIC_MODEL`. The legacy harness `scripts/test_milestone_risk_analyst.py` is kept as a reference for the API call shape; it has hardcoded input paths and the older model `claude-sonnet-4-5` (which Anthropic lists as retiring no sooner than 2026-09-29), so it needs editing before it will run. The known answers in the design summary (ATO not flagged; CDR flagged with RISK-021 removed) came from the design conversation, so re-run both cases after any prompt or model change.
- The prompt names the exact JSON keys. The schema still tolerates case and space variants, keeps unrecognized fields, and the retry message names the expected keys.
- The Analyst's output cap is 4096 tokens (`max_tokens` in `app/agents.py`). Its JSON runs about 2,000 tokens, and the earlier 2048 default cut it off.

## Known limitations

**Update:** Workflow 3 replaces the template path described in the next two paragraphs. It builds the deck with Claude, so a light or heavy week no longer breaks it, but it produces a draft a person reviews and finishes, not a final deck, and some runs need a layout repair round. The history below is why the template approach was dropped.

**(Historical, Workflow 2) The slide deck cannot be fully automated with the current approach, and that's a structural finding, not a bug to fix.** Google Slides' `replaceAllText` (what n8n's Replace Text operation calls under the hood) is pure text substitution — it can swap the text inside an existing placeholder, but it has no concept of inserting or removing a bullet paragraph. Any template built this way needs a fixed number of bullet slots decided when you design the template, while the Executor's generated paragraph is inherently variable length (1–3 sentences per its own instructions, occasionally more). The result is unavoidable either way: fewer sentences than slots leaves visible blank bullets, more sentences than slots silently drops content with no error. Reaching a deck that actually grows and shrinks with the week's content requires calling the Slides API at the paragraph/object level (`insertText` + `createParagraphBullets` per bullet, targeting a shape's object ID) instead of a global find-and-replace — a materially different and larger integration than what's built here.

Given that, this pipeline deliberately stops short of that build-out. The slide-deck path exists to demonstrate what the last mile of "AI-generated weekly status" actually takes with today's low-code tooling, not to ship a production deck generator. If a weekly slide deck is genuinely wanted as a deliverable, the realistic options are: a person does final slide assembly each week using the generated bullets as raw material, or someone invests in the object-level Slides API work above. See [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md) for the full reasoning and the rest of what broke and got fixed along the way.

Three smaller, deliberate loose ends, left as-is because the right call is subjective:
- Workflow 1's own Gmail node (report text, no deck) is deactivated but not deleted, in case sending both a text-only email *and* the combined report+deck email turns out to be preferred over just the one combined email — that's a client/leadership call, not an engineering one.
- The Customer Demo (DEMO) milestone has no independently-verified "known answer" the way ATO and CDR do (see the Analyst instructions section above); its output hasn't been checked against a hand-derived expected result the way the other two have.
- **A decline at the approval gate is currently a dead end.** The `Approved?` IF node's false branch is unconnected — declining just ends the run, with no record of why and nothing sent to anyone. That's fine for a demo, but it papers over a real question: since the report's content is entirely a function of the source data (Asana tasks/risks, the milestone reference, `program_notes.txt`), a decline almost always means the *source data*, not the pipeline's output, needs to change — simply re-running against the same inputs would just regenerate the same report. The more honest version of this gate would have the false branch capture the approver's decline reason (n8n's "Send and Wait for Response" node already collects a free-text comment along with the approve/decline click, so this is data the gate already has and currently throws away) and route it to whoever actually owns the source documents — a Slack message or email asking for the underlying data to be corrected, rather than asking the pipeline to try again. Left unbuilt here because who owns that data, and how they'd want to be notified, is specific to a real deployment, not something to invent unprompted.

## What is not done yet

- Workflow 3 was tested through to a send to the reviewer's own address. Not tested: the decline branch, Workflow 1 calling Workflow 3 (the Asana trial ended first), the Slack-button variant, and data much larger than four milestones (cost per call should grow about linearly, but that is an expectation, not a measurement).
- Replacing the Asana nodes with CSV input was considered and deferred; Asana is the intended source and each user supplies their own paid subscription.
- A decline of the deck approval hands the draft to the reviewer but does not record why.
- The Planner, the other three Executors and the Reviewer described in the original design were never built — this pipeline only implements the Analyst, Executor and Slide title agents. Each additional one would be one instructions file plus one `AgentSpec` in `app/agents.py`.
- Not verified here: the Docker image build and the n8n container running against it end-to-end in a from-scratch environment (this was built and run against an already-running n8n instance).
