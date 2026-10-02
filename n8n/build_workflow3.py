"""Generate the Workflow 3 import files from one node definition (same directory as this script):
  workflow-3-deck-review.json                 Slack notices + Gmail approval link (recommended; works with a local n8n)
  alternatives/workflow-3-slack-buttons.json  Slack notices + Slack approval buttons (needs a public HTTPS n8n)
  alternatives/workflow-3-gmail-only.json     Gmail only

Modeled on the exported Workflow 1 and 2: same node types and versions, same approval pattern
($json.data.approved). The output holds NO credentials and NO personal identifiers: after import, reconnect
your own Slack, Gmail and Header Auth credentials and replace the YOUR_... placeholders (here, in the Config
node, or in the generated files). Pass --with-credentials to keep the credential references set below.
Run: python n8n/build_workflow3.py"""
import json, uuid, pathlib, re, sys

SLACK = {"slackApi": {"id": "YOUR_SLACK_CREDENTIAL_ID", "name": "Slack account"}}
GMAIL = {"gmailOAuth2": {"id": "YOUR_GMAIL_CREDENTIAL_ID", "name": "Gmail account"}}
HDR = {"httpHeaderAuth": {"id": "YOUR_HEADER_AUTH_CREDENTIAL_ID", "name": "Header Auth account"}}
SLACK_USER_ID = "YOUR_SLACK_USER_ID"        # the approver's Slack member ID (Slack profile > Copy member ID)
REVIEWER_EMAIL = "YOUR_EMAIL@example.com"
nodes, conns = [], {}
_n = 0


def add(name, type_, ver, params, pos, creds=None, **extra):
    global _n; _n += 1
    n = {"parameters": params, "type": type_, "typeVersion": ver, "position": pos,
         "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "c2-w3/" + name)), "name": name}
    if creds: n["credentials"] = creds
    n.update(extra); nodes.append(n)


def link(a, b, out=0, idx=0):
    conns.setdefault(a, {"main": []})
    m = conns[a]["main"]
    while len(m) <= out: m.append([])
    m[out].append({"node": b, "type": "main", "index": idx})


CFG = "$('Config').first().json"
BUILD = "$('Build deck').first().json"

add("When Executed by Another Workflow", "n8n-nodes-base.executeWorkflowTrigger", 1.2, {"inputSource": "passthrough"}, [-1500, 0])
add("Manual run", "n8n-nodes-base.manualTrigger", 1, {}, [-1500, 200])
def setf(name, val, typ="string"):
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "f/" + name)), "name": name, "value": val, "type": typ}
add("Config", "n8n-nodes-base.set", 3.4, {"assignments": {"assignments": [
    setf("date", "={{ $json.date || $today.toISODate() }}"),
    setf("reviewer_email", REVIEWER_EMAIL),
    setf("slack_approver_id", SLACK_USER_ID),
    setf("distro_emails", ""),                      # comma separated; EMPTY until you configure it
    setf("distro_send_enabled", False, "boolean"),  # keep false until you have tested end to end
]}, "options": {}}, [-1280, 100])
add("Read sidecar", "n8n-nodes-base.readWriteFile", 1.1, {"fileSelector": "=/files/report_{{ $json.date }}.json", "options": {}}, [-1060, 100])
add("Extract sidecar", "n8n-nodes-base.extractFromFile", 1.1, {"operation": "fromJson", "options": {}}, [-840, 100])
add("read milestones", "n8n-nodes-base.readWriteFile", 1.1, {"fileSelector": "/files/milestones.xlsx", "options": {}}, [-620, 100])
add("Extract milestones", "n8n-nodes-base.extractFromFile", 1.1, {"operation": "xlsx", "options": {}}, [-400, 100])
add("Aggregate rows", "n8n-nodes-base.aggregate", 1, {"aggregate": "aggregateAllItemData", "destinationFieldName": "milestone_rows", "options": {}}, [-180, 100])
add("Build deck", "n8n-nodes-base.httpRequest", 4.5, {
    "method": "POST", "url": "http://deck-service:8010/render_weekly_deck",
    "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
    "sendBody": True, "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify({ sidecar: $('Extract sidecar').first().json, milestone_rows: $json.milestone_rows }) }}",
    "options": {"timeout": 1200000}}, [40, 100], HDR, onError="continueErrorOutput", retryOnFail=False)
add("Notify build failed", "n8n-nodes-base.slack", 2.7, {
    "select": "user", "user": {"__rl": True, "value": f"={{{{ {CFG}.slack_approver_id }}}}", "mode": "id"},
    "text": f"=Weekly deck build FAILED for {{{{ {CFG}.date }}}}. Nothing was sent to anyone. Error: {{{{ $json.error?.message || 'unknown' }}}}",
    "otherOptions": {}}, [280, 300], SLACK)
add("Read draft pptx", "n8n-nodes-base.readWriteFile", 1.1, {"fileSelector": f"={{{{ {BUILD}.files.pptx }}}}", "options": {"dataPropertyName": "pptx"}}, [280, -60])
add("Read draft pdf", "n8n-nodes-base.readWriteFile", 1.1, {"fileSelector": f"={{{{ {BUILD}.files.pdf }}}}", "options": {"dataPropertyName": "pdf"}}, [280, 100])
add("Merge draft files", "n8n-nodes-base.merge", 3.2, {"mode": "combine", "combineBy": "combineByPosition", "options": {}}, [500, 20])
SUMMARY = (f"Status: {{{{ {BUILD}.status }}}}\nSlides: {{{{ {BUILD}.slide_count }}}}\n"
           f"Failed automated checks: {{{{ {BUILD}.failed_checks.length ? {BUILD}.failed_checks.join(', ') : 'none' }}}}\n"
           f"Draft saved at: {{{{ {BUILD}.files.pptx }}}}")
add("Email draft to reviewer", "n8n-nodes-base.gmail", 2.2, {
    "sendTo": f"={{{{ {CFG}.reviewer_email }}}}",
    "subject": f"=[DRAFT] C2 Weekly Leadership Deck {{{{ {CFG}.date }}}}",
    "emailType": "text",
    "message": "=DRAFT for your review. Not yet sent to anyone else.\n\n" + SUMMARY,
    "options": {"attachmentsUi": {"attachmentsBinary": [{"property": "pptx"}, {"property": "pdf"}]}}}, [720, 20], GMAIL)
add("Request deck approval", "n8n-nodes-base.slack", 2.7, {
    "operation": "sendAndWait",
    "user": {"__rl": True, "value": f"={{{{ {CFG}.slack_approver_id }}}}", "mode": "id"},
    "message": "=Weekly leadership deck draft for {{ " + CFG + ".date }} was emailed to you.\n\n" + SUMMARY +
               "\n\nSend to the distribution list exactly as drafted, or edit it yourself first?",
    "approvalOptions": {"values": {"approvalType": "double", "approveLabel": "Send to distribution as-is",
                                   "disapproveLabel": "I'll edit and send myself"}},
    "captureResponder": True, "approvers": [],
    "options": {"limitWaitTime": {"values": {"resumeAmount": 72}}}}, [940, 20], SLACK)
add("Approved?", "n8n-nodes-base.if", 2.3, {"conditions": {
    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 3},
    "conditions": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "c/appr")), "leftValue": "={{ $json.data.approved }}",
                    "rightValue": True, "operator": {"type": "boolean", "operation": "equals"}}],
    "combinator": "and"}, "options": {}}, [1160, 20])
add("Slack: you will send", "n8n-nodes-base.slack", 2.7, {
    "select": "user", "user": {"__rl": True, "value": f"={{{{ {CFG}.slack_approver_id }}}}", "mode": "id"},
    "text": f"=Nothing was sent. The draft is at {{{{ {BUILD}.files.pptx }}}} (and the PDF beside it). Edit and send it yourself.",
    "otherOptions": {}}, [1380, 200], SLACK)
add("Distribution enabled?", "n8n-nodes-base.if", 2.3, {"conditions": {
    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 3},
    "conditions": [
        {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "c/en")), "leftValue": f"={{{{ {CFG}.distro_send_enabled }}}}",
         "rightValue": True, "operator": {"type": "boolean", "operation": "equals"}},
        {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "c/list")), "leftValue": f"={{{{ {CFG}.distro_emails }}}}",
         "rightValue": "", "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}],
    "combinator": "and"}, "options": {}}, [1380, -100])
add("Slack: test mode", "n8n-nodes-base.slack", 2.7, {
    "select": "user", "user": {"__rl": True, "value": f"={{{{ {CFG}.slack_approver_id }}}}", "mode": "id"},
    "text": "=Test mode: you approved, but distribution is disabled or the list is empty in the Config node. Nothing was sent.",
    "otherOptions": {}}, [1600, 40], SLACK)
add("Read release pptx", "n8n-nodes-base.readWriteFile", 1.1, {"fileSelector": f"={{{{ {BUILD}.files.pptx }}}}", "options": {"dataPropertyName": "pptx"}}, [1600, -260])
add("Read release pdf", "n8n-nodes-base.readWriteFile", 1.1, {"fileSelector": f"={{{{ {BUILD}.files.pdf }}}}", "options": {"dataPropertyName": "pdf"}}, [1600, -120])
add("Merge release files", "n8n-nodes-base.merge", 3.2, {"mode": "combine", "combineBy": "combineByPosition", "options": {}}, [1820, -190])
add("Send to distribution", "n8n-nodes-base.gmail", 2.2, {
    "sendTo": f"={{{{ {CFG}.distro_emails }}}}",
    "subject": f"=C2 Weekly Leadership Deck {{{{ {CFG}.date }}}}",
    "emailType": "text",
    "message": f"=Attached is the weekly leadership summary deck for {{{{ {CFG}.date }}}} (PowerPoint and PDF).",
    "options": {"attachmentsUi": {"attachmentsBinary": [{"property": "pptx"}, {"property": "pdf"}]}}}, [2040, -190], GMAIL)
add("Slack: sent", "n8n-nodes-base.slack", 2.7, {
    "select": "user", "user": {"__rl": True, "value": f"={{{{ {CFG}.slack_approver_id }}}}", "mode": "id"},
    "text": f"=Sent the {{{{ {CFG}.date }}}} deck to the distribution list exactly as drafted ({{{{ {BUILD}.files.pptx }}}}).",
    "otherOptions": {}}, [2260, -190], SLACK)

for a, b in [("When Executed by Another Workflow", "Config"), ("Manual run", "Config"), ("Config", "Read sidecar"),
             ("Read sidecar", "Extract sidecar"), ("Extract sidecar", "read milestones"),
             ("read milestones", "Extract milestones"), ("Extract milestones", "Aggregate rows"),
             ("Aggregate rows", "Build deck"), ("Read draft pptx", "Merge draft files"),
             ("Read draft pdf", "Merge draft files"), ("Merge draft files", "Email draft to reviewer"),
             ("Email draft to reviewer", "Request deck approval"), ("Request deck approval", "Approved?"),
             ("Distribution enabled?", "Read release pptx"), ("Distribution enabled?", "Read release pdf"),
             ("Merge release files", "Send to distribution"), ("Send to distribution", "Slack: sent")]:
    link(a, b, 0, 1 if (a, b) in (("Read draft pdf", "Merge draft files"), ("Read release pdf", "Merge release files")) else 0)
link("Read release pptx", "Merge release files", 0, 0); link("Read release pdf", "Merge release files", 0, 1)
link("Build deck", "Read draft pptx", 0); link("Build deck", "Read draft pdf", 0); link("Build deck", "Notify build failed", 1)
link("Approved?", "Distribution enabled?", 0); link("Approved?", "Slack: you will send", 1)
link("Distribution enabled?", "Slack: test mode", 1)

import copy

# Default: Slack. The approval is a DM to the reviewer, and only that user may click the buttons.
for n in nodes:
    if n["type"] == "n8n-nodes-base.slack" and n["parameters"].get("operation") == "sendAndWait":
        n["parameters"]["approvers"] = [SLACK_USER_ID]


def to_gmail(nodes, conns):
    """Alternative with no Slack: the approval and notices go through Gmail (Gmail 'Send and wait' returns the
    same $json.data.approved)."""
    nodes, conns = copy.deepcopy(nodes), copy.deepcopy(conns)
    for n in nodes:
        if n["type"] != "n8n-nodes-base.slack":
            continue
        pr = n["parameters"]; text = pr.get("text") or pr.get("message")
        n["type"] = "n8n-nodes-base.gmail"; n["typeVersion"] = 2.2; n["credentials"] = GMAIL
        n["name"] = n["name"].replace("Slack: ", "Email: ")
        if pr.get("operation") == "sendAndWait":
            n["parameters"] = {"operation": "sendAndWait", "sendTo": f"={{{{ {CFG}.reviewer_email }}}}",
                               "subject": f"=APPROVAL NEEDED: C2 weekly deck {{{{ {CFG}.date }}}}",
                               "message": text.replace("\n", "<br>"),
                               "approvalOptions": pr["approvalOptions"],
                               "options": pr["options"]}
        else:
            n["parameters"] = {"sendTo": f"={{{{ {CFG}.reviewer_email }}}}", "subject": f"=C2 weekly deck {{{{ {CFG}.date }}}}: notice",
                               "emailType": "text", "message": text, "options": {}}
    rename = {"Slack: you will send": "Email: you will send", "Slack: test mode": "Email: test mode", "Slack: sent": "Email: sent"}
    txt = json.dumps({"nodes": nodes, "connections": conns})
    for old, new in rename.items():
        txt = txt.replace(old, new)
    _d = json.loads(txt); nodes[:] = _d["nodes"]; conns.clear(); conns.update(_d["connections"])


    return nodes, conns


def to_hybrid(nodes, conns):
    """Slack for every notice (incl. a 'draft ready' heads-up); only the approval click goes through Gmail,
    because Slack buttons need a public HTTPS URL that a localhost n8n does not have."""
    nodes, conns = copy.deepcopy(nodes), copy.deepcopy(conns)
    for n in nodes:
        if n["name"] == "Request deck approval":
            pr = n["parameters"]
            n["type"] = "n8n-nodes-base.gmail"; n["typeVersion"] = 2.2; n["credentials"] = GMAIL
            n["parameters"] = {"operation": "sendAndWait", "sendTo": f"={{{{ {CFG}.reviewer_email }}}}",
                               "subject": f"=APPROVAL NEEDED: C2 weekly deck {{{{ {CFG}.date }}}}",
                               "message": pr["message"].replace("\n", "<br>").replace("was emailed to you", "is attached to the earlier draft email"),
                               "approvalOptions": pr["approvalOptions"], "options": pr["options"]}
    slack_note = {
        "parameters": {"select": "user", "user": {"__rl": True, "value": f"={{{{ {CFG}.slack_approver_id }}}}", "mode": "id"},
                       "text": f"=Weekly deck draft for {{{{ {CFG}.date }}}} is ready ({{{{ {BUILD}.slide_count }}}} slides, failed checks: "
                               f"{{{{ {BUILD}.failed_checks.length ? {BUILD}.failed_checks.join(', ') : 'none' }}}}). "
                               "Check your email: the approval link is there. Nothing has been sent to anyone else.",
                       "otherOptions": {}},
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "c/slack-ready")), "name": "Slack: draft ready",
        "type": "n8n-nodes-base.slack", "typeVersion": 2.7, "position": [830, 200], "credentials": SLACK}
    nodes.append(slack_note)
    conns["Email draft to reviewer"] = {"main": [[{"node": "Slack: draft ready", "type": "main", "index": 0}]]}
    conns["Slack: draft ready"] = {"main": [[{"node": "Request deck approval", "type": "main", "index": 0}]]}
    return nodes, conns


def write(name, nodes, conns, rel):
    wf = {"name": name, "nodes": copy.deepcopy(nodes), "connections": conns, "pinData": {}, "active": False,
          "settings": {"executionOrder": "v1", "binaryMode": "separate"}, "tags": []}
    if "--with-credentials" not in sys.argv:
        for n in wf["nodes"]:
            n.pop("credentials", None)
    out = pathlib.Path(__file__).resolve().parent / rel
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n"); print("wrote", out, len(nodes), "nodes")


write("Deck Review and Distribute", *to_hybrid(nodes, conns), "workflow-3-deck-review.json")
write("Deck Review and Distribute (Slack buttons)", nodes, conns, "alternatives/workflow-3-slack-buttons.json")
g_nodes, g_conns = to_gmail(nodes, conns)
write("Deck Review and Distribute (Gmail only)", g_nodes, g_conns, "alternatives/workflow-3-gmail-only.json")
