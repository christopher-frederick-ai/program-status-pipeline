// n8n Code node, mode "Run Once for All Items". Name the node: Map tasks
// Turns Asana tasks into the flat rows the agent service expects. The keys match the CSV import
// exactly, so the Analyst sees the same shape it was tuned on.
//
// Works whether the previous node is the native Asana node (one item per task) or an HTTP Request
// to the Asana API (one item holding a `data` list).
const rows = $input.all().flatMap((item) => (Array.isArray(item.json.data) ? item.json.data : [item.json]));

const clean = (v) => (v === null || v === undefined ? '' : String(v).trim());

// Value of a custom field by its name in Asana (text, dropdown or number).
function field(task, name) {
  const f = (task.custom_fields ?? []).find((c) => clean(c.name).toLowerCase() === name.toLowerCase());
  if (!f) return '';
  return clean(f.display_value ?? f.text_value ?? f.enum_value?.name ?? f.number_value);
}

// The board column (section) the task sits in.
function section(task) {
  const m = (task.memberships ?? []).find((x) => clean(x.section?.name));
  return clean(m?.section?.name);
}

const mapped = rows
  .filter((t) => clean(t.name))
  .map((t) => ({
    'Task Name': clean(t.name),
    'Notes': clean(t.notes),
    'Assignee': clean(t.assignee?.name) || field(t, 'Assignee'),
    'Due Date': clean(t.due_on) || clean(t.due_at).slice(0, 10),
    'Section/Column': section(t),
    'Related Milestone': field(t, 'Related Milestone'),
  }));

if (mapped.length === 0) {
  throw new Error('No tasks came back from Asana. Check the project chosen on the Asana node.');
}
if (mapped.every((r) => !r['Related Milestone'])) {
  throw new Error('No task has a "Related Milestone" value. Check that the custom field exists in Asana and that the name matches exactly.');
}

return mapped.map((json) => ({ json }));
