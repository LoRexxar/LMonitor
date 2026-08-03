/**
 * Profile management API contract test
 * Verifies profile management uses dedicated /api/simc-profile/ endpoints
 * Run: node static/dashboard/js/test_profile_api_contract.js
 */

const fs = require('fs');
const path = require('path');

const mainJsPath = path.join(__dirname, 'main.js');
const mainJsContent = fs.readFileSync(mainJsPath, 'utf-8');
const templatePath = path.join(__dirname, '../../../templates/dashboard/index.html');
const templateContent = fs.readFileSync(templatePath, 'utf-8');

// Extract profile management API functions (excludes helper/UI functions)
const profileFunctions = [
    'loadSimcWorkbenchProfiles',
    'simcWbSaveProfile',
    'simcWbEditProfile',
    'simcWbDeleteProfile',
    'simcWbSaveCurrentSimulatorProfile',
    'simcWbFetchProfilesForWorkbench'
];

// Extract only the API-calling functions
const funcBlocks = [];
for (const funcName of profileFunctions) {
    const funcStart = mainJsContent.indexOf(`async function ${funcName}(`);
    const altStart = mainJsContent.indexOf(`function ${funcName}(`);
    const startIdx = funcStart !== -1 ? funcStart : altStart;

    if (startIdx === -1) continue;

    // Find the end of this function
    let braceCount = 0;
    let inFunction = false;
    let endIdx = startIdx;

    for (let i = startIdx; i < mainJsContent.length; i++) {
        if (mainJsContent[i] === '{') {
            braceCount++;
            inFunction = true;
        } else if (mainJsContent[i] === '}') {
            braceCount--;
            if (inFunction && braceCount === 0) {
                endIdx = i + 1;
                break;
            }
        }
    }

    funcBlocks.push(mainJsContent.substring(startIdx, endIdx));
}

const profileBlock = funcBlocks.join('\n\n');

let passed = 0;
let failed = 0;

console.log('Testing profile management API contract...\n');

// Test 1: No generic /dashboard/ CRUD
if (!profileBlock.includes('/dashboard/') && !profileBlock.includes('table_name') && !profileBlock.includes('SimcProfile')) {
    console.log('✓ Does not use /dashboard/ generic CRUD endpoint');
    passed++;
} else {
    console.log('✗ Still uses /dashboard/ generic CRUD endpoint');
    failed++;
}

// Test 2: Uses GET /api/simc-profile/
if (profileBlock.includes("fetch('/api/simc-profile/',") && profileBlock.includes("method: 'GET'")) {
    console.log('✓ Uses GET /api/simc-profile/ for listing');
    passed++;
} else {
    console.log('✗ Missing GET /api/simc-profile/ for listing');
    failed++;
}

// Test 3: Uses GET /api/simc-profile/${id}/
if (profileBlock.includes('/api/simc-profile/${id}/') || profileBlock.includes('/api/simc-profile/${profile') || profileBlock.match(/\/api\/simc-profile\/\$\{[^}]+\}\//)) {
    console.log('✓ Uses GET /api/simc-profile/${id}/ for single fetch');
    passed++;
} else {
    console.log('✗ Missing GET /api/simc-profile/${id}/ for single fetch');
    failed++;
}

// Test 4: Uses PUT for update
if (profileBlock.includes("method: 'PUT'")) {
    console.log('✓ Uses PUT /api/simc-profile/ for updates');
    passed++;
} else {
    console.log('✗ Missing PUT /api/simc-profile/ for updates');
    failed++;
}

// Test 5: Uses POST for create
if (profileBlock.includes("method: 'POST'") && profileBlock.includes('/api/simc-profile/')) {
    console.log('✓ Uses POST /api/simc-profile/ for creation');
    passed++;
} else {
    console.log('✗ Missing POST /api/simc-profile/ for creation');
    failed++;
}

// Test 6: Client-side filtering
if (profileBlock.includes('simcWbProfileSpecFilter') && profileBlock.includes('.filter(')) {
    console.log('✓ Implements client-side spec filtering');
    passed++;
} else {
    console.log('✗ Missing client-side spec filtering');
    failed++;
}

// Test 7: Client-side pagination
if (profileBlock.includes('.slice(') && profileBlock.includes('startIdx') && profileBlock.includes('endIdx')) {
    console.log('✓ Implements client-side pagination');
    passed++;
} else {
    console.log('✗ Missing client-side pagination');
    failed++;
}

// Test 8: Workbench saved-profile loader is defined and uses the dedicated API.
if (profileBlock.includes('async function simcWbFetchProfilesForWorkbench') && profileBlock.includes("fetch('/api/simc-profile/'")) {
    console.log('✓ Provides the workbench saved-profile loader');
    passed++;
} else {
    console.log('✗ Missing workbench saved-profile loader');
    failed++;
}

// Test 9: Active rows expose the dedicated DELETE lifecycle action.
if (profileBlock.includes("method: 'DELETE'") && mainJsContent.includes('data-profile-row-action="delete"')) {
    console.log('✓ Uses DELETE /api/simc-profile/ for profile deletion');
    passed++;
} else {
    console.log('✗ Missing profile deletion action or DELETE request');
    failed++;
}

// Test 10: Ambiguous class specs use canonical keys in the edit form.
const canonicalAmbiguousSpecs = [
    'frost_death_knight', 'frost_mage',
    'protection_paladin', 'protection_warrior',
    'holy_paladin', 'holy_priest',
    'restoration_druid', 'restoration_shaman'
];
const missingCanonicalSpecs = canonicalAmbiguousSpecs.filter(spec => !templateContent.includes(`value="${spec}"`));
if (missingCanonicalSpecs.length === 0) {
    console.log('✓ Profile edit form contains canonical ambiguous spec keys');
    passed++;
} else {
    console.log('✗ Missing canonical spec options: ' + missingCanonicalSpecs.join(', '));
    failed++;
}

// Test 11: Filtering accepts canonical class_spec rows from every Profile source,
// not only rows marked as SimC upstream system profiles.
const matcherStart = mainJsContent.indexOf('function simcProfileMatchesSpecFilter(');
let matcherBlock = '';
if (matcherStart !== -1) {
    let depth = 0;
    let opened = false;
    for (let i = matcherStart; i < mainJsContent.length; i++) {
        if (mainJsContent[i] === '{') { depth++; opened = true; }
        if (mainJsContent[i] === '}') {
            depth--;
            if (opened && depth === 0) { matcherBlock = mainJsContent.slice(matcherStart, i + 1); break; }
        }
    }
}
let canonicalFilteringWorks = false;
try {
    const matcher = Function(`${matcherBlock}; return simcProfileMatchesSpecFilter;`)();
    canonicalFilteringWorks =
        matcher({spec: 'rogue_subtlety', class_name: 'rogue', is_system: false}, 'subtlety') &&
        matcher({spec: 'rogue_subtlety', class_name: 'rogue_subtlety', is_system: false}, 'subtlety') &&
        matcher({spec: 'mage_arcane', class_name: 'mage', is_system: false}, 'arcane') &&
        matcher({spec: 'mage_frost', class_name: 'mage', is_system: false}, 'frost_mage') &&
        !matcher({spec: 'deathknight_frost', class_name: 'deathknight', is_system: false}, 'frost_mage') &&
        matcher({spec: 'fury', class_name: '', is_system: false}, 'fury');
} catch (_) {
    canonicalFilteringWorks = false;
}
if (canonicalFilteringWorks) {
    console.log('✓ Profile filtering accepts canonical class_spec rows from all sources');
    passed++;
} else {
    console.log('✗ Profile filtering drops canonical class_spec rows from non-system sources');
    failed++;
}

console.log(`\n${passed}/11 tests passed`);

if (failed > 0) {
    console.log(`\n❌ ${failed} test(s) failed`);
    process.exit(1);
} else {
    console.log('\n✅ All tests passed!');
    process.exit(0);
}
