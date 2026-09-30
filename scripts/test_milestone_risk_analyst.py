"""
Standalone test harness for the Milestone & Risk Analyst agent instructions.

What this does:
  - Loads the LLM instructions (system prompt) from LLM_instructions.docx
  - Builds a per-milestone data cluster from the 4 source files (milestone
    schedule, task tracker, risk register, engineering notes)
  - Calls the Anthropic API once per cluster and prints the JSON result

Requires: pip install anthropic python-docx openpyxl --break-system-packages
Requires: ANTHROPIC_API_KEY set in the environment.

Usage:
  python3 test_milestone_risk_analyst.py ATO
  python3 test_milestone_risk_analyst.py CDR
"""

import csv
import json
import sys

import docx
import openpyxl
from anthropic import Anthropic

INSTRUCTIONS_PATH = "/mnt/user-data/outputs/LLM_instructions_v2_with_status.docx"
MILESTONES_XLSX = "/mnt/user-data/outputs/milestones.xlsx"
TASKS_CSV = "/mnt/user-data/outputs/asana_task_tracker_import.csv"
RISKS_CSV = "/mnt/user-data/outputs/asana_risk_register_import.csv"
NOTES_TXT = "/mnt/user-data/outputs/program_notes.txt"

MILESTONE_NAMES = {
    "ATO": "Authority to Operate (ATO) Approval",
    "CDR": "Critical Design Review (CDR)",
}


def load_instructions():
    d = docx.Document(INSTRUCTIONS_PATH)
    return "\n".join(p.text for p in d.paragraphs if p.text.strip())


def load_milestone_row(name):
    wb = openpyxl.load_workbook(MILESTONES_XLSX)
    ws = wb["Milestones"]
    rows = list(ws.iter_rows(values_only=True))
    header, body = rows[0], rows[1:]
    for row in body:
        if row[0] == name:
            return dict(zip(header, row))
    return None


def load_csv_rows(path, milestone_name):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("Related Milestone") == milestone_name]


def build_cluster(key):
    name = MILESTONE_NAMES[key]
    return {
        "milestone_reference": load_milestone_row(name),
        "task_tracker": load_csv_rows(TASKS_CSV, name),
        "risk_register": load_csv_rows(RISKS_CSV, name),
        "engineering_notes": open(NOTES_TXT).read(),
    }


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "ATO"
    system_prompt = load_instructions()
    cluster = build_cluster(key)

    client = Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": "Source data:\n" + json.dumps(cluster, indent=2),
            }
        ],
    )
    print(resp.content[0].text)


if __name__ == "__main__":
    main()
