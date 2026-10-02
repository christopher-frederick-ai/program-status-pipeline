# Workflow 3: Weekly deck with human approval (replaces Workflow 2)

Build as a NEW workflow. Deactivate Workflow 2 (do not delete; the repo keeps it in `n8n/legacy/`). Workflow 1 changes only in that its `Call 'Slide Build'` node now points at this workflow.
Principle: the deck is generated once. Release sends the saved file by path; nothing regenerates between
draft and send.

## Settings (n8n Variables or the Set node 1, not hard-coded in nodes)
`REVIEWER_EMAIL`, `DISTRO_EMAIL_LIST` (comma separated), `DISTRO_SEND_ENABLED` (default `false`),
`SLACK_APPROVAL_CHANNEL`. Credential: header auth `X-Service-Token` = `SERVICE_SHARED_SECRET`.

## Nodes
1. **Trigger**: Execute Workflow Trigger from W1's final approved step (input `date`), plus a Manual Trigger
   with a `date` field for reruns. If W1 does not call it, use Schedule after W1's usual time.
2. **Set: config**: `date` (YYYY-MM-DD), `sidecarPath` = `/files/report_{{date}}.json`,
   `milestonesPath` = `/files/milestones.xlsx`, reviewer/distro/flag from Settings.
3. **Read/Write Files from Disk**: read `sidecarPath` → **Extract From File** (JSON) → field `sidecar`.
   Fail loudly if missing (the report was not approved/saved).
4. **Read/Write Files from Disk** `milestonesPath` → **Extract From File** (XLSX) → rows.
5. **Merge/Code-free assembly**: use an **Edit Fields (Set)** node producing
   `{ "sidecar": {{ $('Extract sidecar').item.json }}, "milestone_rows": {{ $('Extract xlsx').all().map(i => i.json) }} }`.
   (Expression only; no logic. The join and validation live in the tested service.)
6. **HTTP Request** POST `http://deck-service:8010/render_weekly_deck`, JSON body from node 5,
   header `X-Service-Token`, **timeout 1,200,000 ms**, retry off (a retry would bill another run),
   On Error: continue (error output).
7. **IF** response `status` exists. Error branch → **Slack** message to the reviewer: "Deck build failed
   (HTTP code, error type). Report was not distributed." Stop.
8. **Read Binary File** ×2: `files.pptx`, `files.pdf` from the response paths.
9. **Gmail** send to `REVIEWER_EMAIL` only: subject `[DRAFT] C2 Weekly Leadership Deck {{date}}`; body states
   `status`, `slide_count`, and `failed_checks` (if `needs_review`, say so first); attach pptx and pdf.
10. **Slack: Send and Wait for Response** (channel/DM to reviewer), approval type "custom form" or buttons:
    "Send to distribution as-is" / "I'll edit and send myself". Message includes the date, status, failed
    checks, and the draft file path. Wait limit 3 days; on timeout nothing is sent.
11. **Switch** on the answer:
    - *as-is* → node 12.
    - *edit myself* → Slack confirmation "Nothing sent. Draft is at <path>." End.
12. **IF `DISTRO_SEND_ENABLED` is true**: yes → node 13; no → Slack "Test mode: would have sent to
    <list>. Nothing sent." End.
13. **Read Binary File** the same saved pptx/pdf **by path from node 6's response** (no new service call) →
    **Gmail** send to `DISTRO_EMAIL_LIST`, subject `C2 Weekly Leadership Deck {{date}}`, attach both.
14. **Slack** confirmation to the reviewer with recipient count. Optionally append a line to a send log
    (`/files/decks/send_log.jsonl`: date, request_id, approver, time; no content).

## Notes
- n8n needs `N8N_RESTRICT_FILE_ACCESS_TO=/files` (already your setting); decks are under `/files/decks`.
- The reviewer who edits the deck sends it themselves, so the as-is path is the only automated distribution.
- Keep W2 off so the old template path cannot also send.

## Importable files
`n8n/workflow-3-deck-review.json` is the one in use: Slack notices plus a "draft ready" DM, with the approval
sent as a Gmail Send and Wait link (Slack buttons need a public HTTPS URL; a localhost n8n cannot receive
them). `n8n/alternatives/workflow-3-slack-buttons.json` (Slack buttons) and
`n8n/alternatives/workflow-3-gmail-only.json` (no Slack) are the alternatives. Differences from the outline:
Config values live in one Set node (`Config`); two Read nodes + Merge attach both files; the build node's
error output goes to a Slack DM. The files carry no credentials or personal identifiers: after import,
reconnect your Slack, Gmail and Header Auth credentials and replace the `YOUR_...` placeholders. Call it
from W1 by repointing `Call 'Slide Build'` at this workflow (or run it manually). Confirm the Header Auth
credential carries the `SERVICE_SHARED_SECRET` value, and leave `distro_send_enabled` false except when
testing with your own address in `distro_emails`. Regenerate with `python n8n/build_workflow3.py`.
