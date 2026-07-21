import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const sourcePath = new URL('../../../static/portal/js/talent_simulator.js', import.meta.url);
const templatePath = new URL('../../../templates/portal/talent_simulator.html', import.meta.url);
const rawSource = fs.readFileSync(sourcePath, 'utf8');
const template = fs.readFileSync(templatePath, 'utf8');
let source = rawSource;
const bindingStart = "    els.importBtn.addEventListener('click', () => {";
const bindingIndex = source.indexOf(bindingStart);
assert.notEqual(bindingIndex, -1, 'talent simulator binding block must exist');
source = `${source.slice(0, bindingIndex)}
    globalThis.__talentRuleTest = {
        state,
        treeGateBlocker,
        treeRuleBlocker,
    };
})();\n`;

const page = {dataset: {defaultVersion: ''}};
const context = {
    console,
    URL,
    URLSearchParams,
    location: {search: ''},
    document: {
        querySelector: selector => selector === '.talent-sim-page' ? page : null,
        getElementById: id => ({textContent: id.endsWith('-data') ? '[]' : ''}),
    },
};
context.window = context;
context.globalThis = context;
vm.runInNewContext(source, context, {filename: 'talent_simulator.js'});

const {state, treeGateBlocker, treeRuleBlocker} = context.__talentRuleTest;

function setNodes(nodes) {
    state.nodes = new Map(nodes.map((node, index) => {
        const normalized = {
            node_key: node.node_key || `${node.tree_type || 'spec'}:${index + 1}`,
            tree_type: node.tree_type || 'class',
            point_pool: node.point_pool || node.tree_type || 'class',
            row: node.row || 1000,
            points: Number(node.points || 0),
            purchased: node.purchased !== false,
            ...node,
        };
        return [normalized.node_key, normalized];
    }));
}

const apexNode = {
    node_key: 'spec:apex',
    tree_type: 'spec',
    point_pool: 'apex',
    is_apex_talent: true,
    row: 9999,
    points: 0,
};

setNodes([
    {node_key: 'spec:spent', tree_type: 'spec', point_pool: 'spec', row: 1000, points: 19},
    apexNode,
]);
assert.match(treeGateBlocker(state.nodes.get('spec:apex')), /专精树前7层需要至少 20 点/);

setNodes([
    {node_key: 'spec:spent', tree_type: 'spec', point_pool: 'spec', row: 1000, points: 20},
    apexNode,
]);
assert.equal(treeGateBlocker(state.nodes.get('spec:apex')), '');

setNodes([
    {node_key: 'spec:spent', tree_type: 'spec', point_pool: 'spec', row: 1000, points: 30},
    {...apexNode, points: 3},
]);
assert.equal(treeRuleBlocker(state.nodes.get('spec:spent'), 1), '');

setNodes([
    {node_key: 'spec:spent', tree_type: 'spec', point_pool: 'spec', row: 1000, points: 30},
    {...apexNode, points: 4},
]);
assert.match(treeRuleBlocker(state.nodes.get('spec:spent'), 1), /专精与顶峰天赋合计最多只能投入 34 点/);

setNodes([
    {node_key: 'spec:spent', tree_type: 'spec', point_pool: 'spec', row: 1000, points: 34},
    apexNode,
]);
assert.match(treeRuleBlocker(state.nodes.get('spec:apex'), 1), /专精与顶峰天赋合计最多只能投入 34 点/);

setNodes([
    {node_key: 'class:spent', tree_type: 'class', point_pool: 'class', row: 1000, points: 34},
]);
assert.match(treeRuleBlocker(state.nodes.get('class:spent'), 1), /职业树最多只能投入 34 点/);

const scheduleEncodeSource = rawSource.slice(
    rawSource.indexOf('    function scheduleEncode()'),
    rawSource.indexOf('    async function encodeCurrent(requestSeq)', rawSource.indexOf('    function scheduleEncode()')),
);
assert.match(scheduleEncodeSource, /invalidateEncode\(\)/, 'a state change must invalidate in-flight encode requests before debounce');
assert.match(scheduleEncodeSource, /正在生成/, 'the export field must immediately expose its pending state');

const invalidateEncodeSource = rawSource.slice(
    rawSource.indexOf('    function invalidateEncode()'),
    rawSource.indexOf('    function scheduleEncode()', rawSource.indexOf('    function invalidateEncode()')),
);
assert.match(invalidateEncodeSource, /state\.encodeRequestSeq \+= 1/, 'invalidating must reject stale encode responses');
assert.match(invalidateEncodeSource, /abort\(\)/, 'invalidating must cancel the in-flight encode request');
assert.match(invalidateEncodeSource, /copyCodeBtn\.disabled = true/, 'invalidating must disable copying stale build strings');

const loadTreeSource = rawSource.slice(
    rawSource.indexOf('    async function loadTree()'),
    rawSource.indexOf('    function indexNodes()', rawSource.indexOf('    async function loadTree()')),
);
assert.match(loadTreeSource, /invalidateEncode\(\)/, 'loading another tree context must invalidate the previous encode request');
assert.match(loadTreeSource, /state\.loadRequestSeq/, 'tree loads must carry their own request generation');
assert.match(loadTreeSource, /loadAbortController\.abort\(\)/, 'loading another tree must cancel the previous tree request');
assert.match(loadTreeSource, /loadRequestSeq !== state\.loadRequestSeq/, 'stale tree responses must not replace the latest context');
assert.match(loadTreeSource, /state\.nodes\.clear\(\)/, 'loading another tree must remove stale node interactions immediately');
assert.match(loadTreeSource, /els\.resetBtn\.disabled = true/, 'tree loading must disable reset actions that can render stale nodes');
assert.ok((loadTreeSource.match(/invalidateEncode\(\)/g) || []).length >= 4, 'success and failure exits must invalidate encodes started during loading');
assert.match(loadTreeSource, /state\.buildCode = String\(data\.build_code \|\| ''\)/, 'a new tree must not retain a stale build string');

const codeOutputBlock = template.match(/<div class="talent-code-output">([\s\S]*?)<\/div>/)?.[1] || '';
assert.match(codeOutputBlock, /id="talent-copy-code-btn"/, 'the copy action must be adjacent to the exported build string');
assert.doesNotMatch(
    template.match(/<div class="talent-sim-actions">([\s\S]*?)<\/div>/)?.[1] || '',
    /id="talent-copy-code-btn"/,
    'the build-string copy action must not be detached in the page header',
);

console.log('talent simulator shared spec/apex and live export rules: ok');
