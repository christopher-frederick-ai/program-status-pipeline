// n8n Code node, mode "Run Once for All Items". Name: Format report
// Runs downstream of "Slide title", with Split Out, Analyst and Executor still reachable on the
// same path (Executor -> Slide title -> Format report). Produces the narrative .txt report
// (unchanged) PLUS a JSON sidecar with the Analyst's structured fields and the Slide title
// agent's title/subtitle per milestone, for the slide-deck workflow to read later with no
// further LLM calls of its own.
const TITLE = 'Meridian Defense Systems C2 Platform Program: Weekly Status Report';
const date = $today.toISODate();

const splitItems = $('Split Out').all();
const analystItems = $('Analyst').all();
const executorItems = $('Executor').all();
const titleItems = $input.all();

if (
  splitItems.length !== analystItems.length ||
  splitItems.length !== executorItems.length ||
  splitItems.length !== titleItems.length
) {
  throw new Error(
    `Item count mismatch: Split Out=${splitItems.length}, Analyst=${analystItems.length}, ` +
    `Executor=${executorItems.length}, Slide title=${titleItems.length}. All four must ` +
    'produce one item per milestone, in the same order.'
  );
}

function shortKey(name) {
  if (name.includes('Authority to Operate')) return 'ATO';
  if (name.includes('Critical Design Review')) return 'CDR';
  if (name.includes('Customer Demo') || name.includes('IOC Readiness')) return 'DEMO';
  return name.replace(/[^A-Za-z0-9]+/g, '_').toUpperCase().slice(0, 12) || 'MILESTONE';
}

const milestones = {};
const milestoneParagraphs = [];

for (let i = 0; i < splitItems.length; i++) {
  const name = String(splitItems[i].json.milestone_reference?.Milestone ?? '').trim();
  const analyst = analystItems[i].json.output ?? {};
  const paragraph = String(executorItems[i].json.output ?? '').trim();
  const titleOut = titleItems[i].json.output ?? {};

  if (paragraph) milestoneParagraphs.push(paragraph);

  milestones[shortKey(name)] = {
    name,
    status: String(analyst.status ?? ''),
    explanation: String(analyst.explanation ?? ''),
    milestone_status_flagged: analyst.milestone_status_flagged === 'yes' ? 'yes' : 'no',
    risks_recommended_for_removal: analyst.risks_recommended_for_removal ?? [],
    executor_text: paragraph,
    slide_title: String(titleOut.title ?? ''),
    slide_subtitle: String(titleOut.subtitle ?? ''),
  };
}

if (milestoneParagraphs.length === 0) {
  throw new Error('No Executor output to format. Check the Executor node before this one.');
}

const sections = [{ title: 'Milestones & Risks', paragraphs: milestoneParagraphs }];
const body = sections.map((s) => `${s.title.toUpperCase()}\n\n${s.paragraphs.join('\n\n')}`).join('\n\n');
const report = `${TITLE}\nReport date: ${date}\n\n${body}\n`;

const sidecar = { title: TITLE, date, milestones };

return [{
  json: {
    report,
    subject: `${TITLE} (${date})`,
    date,
    milestone_count: milestoneParagraphs.length,
    sidecar_json: JSON.stringify(sidecar, null, 2),
  },
}];
