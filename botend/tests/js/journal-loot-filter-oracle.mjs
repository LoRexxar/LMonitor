import fs from 'node:fs';
import vm from 'node:vm';
vm.runInThisContext(fs.readFileSync('static/portal/js/journal-loot-filter.js', 'utf8'));
const {rows, filters} = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(filters.map(filter => rows.filter(row => JournalLootFilter(row, filter)).map(row => row.item_id))));
