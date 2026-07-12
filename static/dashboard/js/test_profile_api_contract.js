/**
 * Profile management API contract test
 * Verifies profile management uses dedicated /api/simc-profile/ endpoints
 * Run: node static/dashboard/js/test_profile_api_contract.js
 */

const fs = require('fs');
const path = require('path');

const mainJsPath = path.join(__dirname, 'main.js');
const mainJsContent = fs.readFileSync(mainJsPath, 'utf-8');

// Extract profile management API functions (excludes helper/UI functions)
const profileFunctions = [
    'loadSimcWorkbenchProfiles',
    'simcWbLaunchSimulation',
    'simcWbSaveProfile',
    'simcWbDeleteProfile',
    'simcWbEditProfile',
    'simcWbLoadProfileToSimulator',
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

// Test 4: Uses POST with simulate_now
if (profileBlock.includes('simulate_now: true')) {
    console.log('✓ Has simulate_now flag for launch simulation');
    passed++;
} else {
    console.log('✗ Missing simulate_now flag for launch simulation');
    failed++;
}

// Test 5: Uses DELETE with JSON body
if (profileBlock.includes("method: 'DELETE'") && profileBlock.includes('body: JSON.stringify({ id:')) {
    console.log('✓ Uses DELETE /api/simc-profile/ with JSON body');
    passed++;
} else {
    console.log('✗ Missing DELETE /api/simc-profile/ with JSON body');
    failed++;
}

// Test 6: Uses PUT for update
if (profileBlock.includes("method: 'PUT'")) {
    console.log('✓ Uses PUT /api/simc-profile/ for updates');
    passed++;
} else {
    console.log('✗ Missing PUT /api/simc-profile/ for updates');
    failed++;
}

// Test 7: Uses POST for create
if (profileBlock.includes("method: 'POST'") && profileBlock.includes('/api/simc-profile/')) {
    console.log('✓ Uses POST /api/simc-profile/ for creation');
    passed++;
} else {
    console.log('✗ Missing POST /api/simc-profile/ for creation');
    failed++;
}

// Test 8: Client-side filtering
if (profileBlock.includes('simcWbProfileSpecFilter') && profileBlock.includes('.filter(')) {
    console.log('✓ Implements client-side spec filtering');
    passed++;
} else {
    console.log('✗ Missing client-side spec filtering');
    failed++;
}

// Test 9: Client-side pagination
if (profileBlock.includes('.slice(') && profileBlock.includes('startIdx') && profileBlock.includes('endIdx')) {
    console.log('✓ Implements client-side pagination');
    passed++;
} else {
    console.log('✗ Missing client-side pagination');
    failed++;
}

// Test 10: Has launch simulation button handler
if (profileBlock.includes('simcWbLaunchSimulation') && profileBlock.includes('simc-wb-profile-simulate')) {
    console.log('✓ Has launch simulation button and handler');
    passed++;
} else {
    console.log('✗ Missing launch simulation button or handler');
    failed++;
}

// Test 11: Workbench saved-profile loader is defined and uses the dedicated API.
if (profileBlock.includes('async function simcWbFetchProfilesForWorkbench') && profileBlock.includes("fetch('/api/simc-profile/'")) {
    console.log('✓ Provides the workbench saved-profile loader');
    passed++;
} else {
    console.log('✗ Missing workbench saved-profile loader');
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
