// n8n Code node, mode "Run Once for All Items". Name the node: Build request
// Gathers everything the Cluster call needs into ONE item. It reads the earlier nodes by name,
// so if you rename a node, change its name here too.
return [{ json: {
  milestones: $('Extract milestones').all().map(i => i.json),
  tasks: $('Map tasks').all().map(i => i.json),
  risks: $('Map risks').all().map(i => i.json),
  engineering_notes: $('Extract notes').first().json.data ?? '',
  as_of_date: $today.toISODate(),
} }];
