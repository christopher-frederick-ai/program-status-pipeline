# Deck service and Workflow 3 (weekly deck with human approval)

`POST /render_weekly_deck` turns one week's vetted analysis into a leadership deck. It calls Claude
with code execution and the pptx skill, using `agents/deck_builder.md` as the instructions. The service
renders the PDF itself, runs automated checks, and sends failures plus page images back to the model
for up to `DECK_MAX_REPAIRS` repair rounds. A deck is always returned if one was produced.
`status: "needs_review"` means checks still fail; a person reviews every deck regardless.

## Request (JSON)
Exactly what Workflow 3 already has, with no Code node in between:

    { "sidecar": <contents of files/report_<date>.json>,
      "milestone_rows": <rows of milestones.xlsx as Extract From XLSX returns them>,
      "program": "optional, default derived from sidecar.title",
      "options": { "min_slides": 5, "max_slides": 10 } }          // optional

`milestone_rows` keys: `Milestone`, `Planned Date`, `Forecast/Actual Date` (date or `TBD`), `Status`.
Dates may arrive as `YYYY-MM-DD` or Excel ISO datetimes. `deck_service/adapter.py` joins the two by exact
milestone name (a sidecar milestone missing from the sheet is a 422, not a silent gap), turns the Analyst's
`milestone_status_flagged` ("yes" = status NOT supported) into `reported_status_supported`, and withholds the
Analyst's `status`/`explanation` notes: the deck says only what the reviewer approved. Older sidecars
without `slide_title`/`slide_subtitle` work. Header: `X-Service-Token`.

## Response
`status`, `files{pptx, pdf, manifest}` (paths on the shared /files mount), `slide_count`, `checks[]`,
`failed_checks[]`, `repair_rounds`, `usage`, `latency_ms`, `request_id`.
Errors: 401 bad token, 503 no `SERVICE_SHARED_SECRET` configured (fails closed), 422 bad input,
502 model call failed or the model never saved a deck (message carries the error type only, never content).

## Configuration (env)
`ANTHROPIC_API_KEY`, `SERVICE_SHARED_SECRET`, `DECK_MODEL` (default `claude-sonnet-5-5`), `DECK_MAX_REPAIRS` (2),
`DECK_MAX_PAUSES` (0), `DECK_MAX_CALLS` (6, hard cap on model calls per request), `DECK_MAX_TOKENS` (20000; the SDK refuses more without streaming), `DECK_OUTPUT_DIR` (`/files/decks`),
`DECK_INSTRUCTIONS_PATH` (`agents/deck_builder.md`). Logs carry sizes and token counts, never content.
Needs LibreOffice (`soffice`) and poppler (`pdftoppm`) on the machine: see `Dockerfile`.

    pip install -r requirements.txt
    SERVICE_SHARED_SECRET=... ANTHROPIC_API_KEY=... uvicorn deck_service.main:app --port 8010
    pytest tests -q          # offline: fake model, no network, no key

## Tuning loop
    python scripts/repeat_deck_runs.py fixtures/week_2026-09-24.json -n 5 --no-repair --label round3
    python scripts/repeat_deck_runs.py fixtures/week_light.json      -n 5 --no-repair --label round3
`--no-repair` measures the instructions alone. Edit `agents/deck_builder.md`, re-run, compare the JSON
records in `runs/`. `scripts/check_deck.py FIXTURE OUTDIR` runs the 21 checks on any saved deck.

## Workflow 3: weekly deck with human approval
Workflow 3 (W3) replaces the fixed-template Google Slides build (Workflow 2). W1 is unchanged except that its
`Call 'Slide Build'` node is repointed at W3. W2 stays in n8n, unused.

    W1 approves and saves files/report_<date>.json
      -> W3 Config -> read sidecar + files/milestones.xlsx -> POST deck-service
      -> Slack DM "draft ready" + email draft (PPTX + PDF) to the reviewer
      -> human approval (approve / decline)
           approve + distribution enabled -> email the SAME saved files to the distribution list -> Slack "sent"
           approve + distribution disabled -> Slack "test mode" notice, nothing sent
           decline -> Slack "you will send" notice; the reviewer edits and sends the draft
      -> build failure at any point -> Slack "build failed" DM, nothing sent

The deck is generated once. Release sends the saved file by path; nothing regenerates between draft and send.
A person approves every deck before anything reaches the distribution list, and nothing is sent by default.

### Which workflow file to import (`../n8n/`, built by `../n8n/build_workflow3.py`)
| File | Notices | Approval click | Use when |
|---|---|---|---|
| `workflow-3-deck-review.json` | Slack | Gmail link | n8n runs on localhost (recommended) |
| `alternatives/workflow-3-slack-buttons.json` | Slack | Slack buttons | n8n has a public HTTPS URL (see below) |
| `alternatives/workflow-3-gmail-only.json` | Gmail | Gmail link | no Slack at all |

The files hold no credentials or personal identifiers. After import, reconnect your Slack, Gmail and Header
Auth credentials, replace the `YOUR_...` placeholders (the Config node's `reviewer_email` and
`slack_approver_id`), and leave the workflow inactive until you call it from W1. The Header Auth credential
must carry the same value as the deck service's `SERVICE_SHARED_SECRET` (header `X-Service-Token`).

### Config node fields
`date` (default `{{ $json.date || $today.toISODate() }}`; for a test, use Fixed text `2026-09-24`, never an
unquoted `{{ 2026-09-24 }}`, which evaluates as arithmetic), `reviewer_email`, `slack_approver_id`,
`distro_emails`, `distro_send_enabled` (boolean, default false). Distribution runs only if the flag is true
AND `distro_emails` is not empty. To test the send path safely, put only your own address in `distro_emails`.

### Slack approval buttons and localhost
Slack "Send and wait" buttons are interactive buttons: Slack posts the click to a public HTTPS URL. A
localhost n8n is not reachable, so the buttons render but a click does not resume the workflow. Gmail
"Send and wait" uses links that open in your own browser, which works on localhost. That is why the hybrid
file exists. To use Slack buttons, give n8n a public HTTPS address (a tunnel or hosted n8n), set
`WEBHOOK_URL`, and add the Interactivity Request URL and signing secret in the Slack app. W1's Slack approval
has the same limitation.

### Cost and run time (measured)
First run: 51 sandbox steps, about 22M input tokens, $46.44, because every step re-sent a growing context
(62k to over 500k tokens), mostly from the model rendering and inspecting its own output. Now: the work
budget in `agents/deck_builder.md`, QA moved into the service's 21 checks, `DECK_MAX_CALLS` 6, no pauses.
Typical run: 1 model call (about 130k input, 15k output, about 2 minutes) when the checks pass, 2 calls
(about 380k input) when a repair round is needed. Roughly $1 to $2 per deck at the time of writing; check
the Console. Cost per call should grow about linearly with data size; that has not been tested beyond 4
milestones. Set a spend limit in the Console.

### Dependencies and known limitations
- **Asana:** W1 reads tasks and risks from Asana over HTTP using the Related Milestone custom field. Custom
  fields need a paid Asana plan, and each user supplies their own Asana subscription and API credential, the
  same way they supply their own Anthropic key. Without it, Map tasks stops with "No task has a Related
  Milestone value". Replacing Asana with CSV input was considered and deferred.
- **Slack buttons** need a public URL (above). W1's approval has the same limit.
- **Layout repairs:** some runs need a repair round for layout checks (one of the two most recent did); the loop handles it.
- Decline path of W3, and the W1 to W3 call, have not been exercised end to end.
- Test data is fictitious; the first-round check results vary run to run.

## Rebuild after changes
    docker compose up -d --build deck-service     # from the repo root, where docker-compose.yml is
    docker compose up -d                          # if n8n is not up on port 5678
    docker compose logs deck-service --tail 20    # per-call tokens and failed check names

See [`../docs/WORKFLOW_3_SPEC.md`](../docs/WORKFLOW_3_SPEC.md) (node by node). The service is defined in the repo's `docker-compose.yml`.

## Verified vs not
Offline (39 tests, fake model): success, repair, exhausted repair, lost files, pause_turn, auth, fail-closed,
validation incl. name mismatch and duplicate rows, call cap, truncated reply, per-call and failed-check log
lines, workflow JSON wiring for all three files, and that the published workflow files hold no credentials, Slack IDs or email addresses, adapter on three real sidecars, older sidecars, light week.
Live (Sept 24 data): model call saves the deck, Dockerfile build, n8n wiring, draft email and Slack DM,
Gmail approval link, release send to the reviewer's own address, Slack "sent" notice.
Not verified: Slack interactive buttons (need a public URL), decline path, W1 calling W3, behavior at much
larger data sizes. `fixtures/week_light.json` is synthetic.
