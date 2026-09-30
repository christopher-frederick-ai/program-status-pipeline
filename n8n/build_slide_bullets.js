// n8n Code node, mode "Run Once for All Items". Name: Build slide bullets
// Workflow 2. Runs after reading report_<date>.json and parsing it (Extract From File,
// operation "Extract From JSON"). Turns each milestone's ALREADY-REVIEWED Executor paragraph
// into up to 5 plain-text bullets for the slide template placeholders.
//
// Deliberately splits `executor_text`, not the Analyst's raw status/explanation fields: the
// Executor paragraph is what the human actually saw and approved in the Slack draft (and could
// have hand-edited in the saved .txt file), and it's already deduplicated -- the Executor's own
// instructions forbid repeating what status/explanation already say. Rebuilding bullets from the
// raw Analyst fields instead would risk saying something the human never reviewed, and would
// likely duplicate content across bullets.
//
// Output: one item with DATE plus, for each milestone key in the sidecar (ATO, CDR, DEMO):
// <KEY>_TITLE and <KEY>_SUBTITLE (from the Slide title agent, already saved in the sidecar --
// no LLM call happens here) and <KEY>_B1..<KEY>_B5. Unused bullet slots are empty strings,
// matching a milestone with less to report. Ready to feed the Google Slides "Replace text in
// a presentation" node.

const MAX_BULLETS = 5;

// Same rule scripts/repeat_runs.py uses, so "R. Chen" etc. don't get split as if the
// period ended a sentence.
function sentences(text) {
  const parts = String(text || '').trim().split(/(?<=[.!?])\s+(?=[A-Z("'])/);
  const out = [];
  for (const p of parts) {
    if (out.length && /\b[A-Z]\.$/.test(out[out.length - 1])) {
      out[out.length - 1] += ' ' + p;
    } else {
      out.push(p);
    }
  }
  return out.filter((s) => s.trim());
}

// "Extract From File" (operation "Extract From JSON") may put the parsed object directly on
// the item, or nest it under a "Destination Key" (commonly "data") depending on that node's
// settings. Accept either shape rather than guessing which one you have.
const raw = $input.first().json;
const sidecar = raw && raw.milestones ? raw : raw && raw.data && raw.data.milestones ? raw.data : null;

if (!sidecar) {
  throw new Error(
    `Could not find "milestones" in the parsed JSON. Top-level keys were: ${Object.keys(raw || {}).join(', ') || '(none)'}. ` +
    'Check the "Destination Key" setting on the Extract From File node, or the file path on the read node before it.'
  );
}

const milestones = sidecar.milestones;
const keys = Object.keys(milestones);

if (keys.length === 0) {
  throw new Error('The "milestones" object was found but is empty. Check report_<date>.json on disk.');
}

const result = { DATE: sidecar.date ?? '' };

for (const key of keys) {
  const m = milestones[key];
  const bullets = sentences(m.executor_text).slice(0, MAX_BULLETS);
  if (bullets.length === 0) {
    throw new Error(`Milestone ${key} (${m.name || 'unnamed'}) has no executor_text to build bullets from.`);
  }
  result[`${key}_TITLE`] = m.slide_title || m.name || key;
  result[`${key}_SUBTITLE`] = m.slide_subtitle || '';
  for (let i = 0; i < MAX_BULLETS; i++) {
    result[`${key}_B${i + 1}`] = bullets[i] ?? '';
  }
}

return [{ json: result }];
