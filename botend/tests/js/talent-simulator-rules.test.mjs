import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const sourcePath = new URL('../../../static/portal/js/talent_simulator.js', import.meta.url);
let source = fs.readFileSync(sourcePath, 'utf8');
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
    {node_key: 'class:spent', tree_type: 'class', point_pool: 'class', row: 1000, points: 22},
    apexNode,
]);
assert.match(treeGateBlocker(state.nodes.get('spec:apex')), /职业树前7层需要至少 23 点/);

setNodes([
    {node_key: 'class:spent', tree_type: 'class', point_pool: 'class', row: 1000, points: 23},
    apexNode,
]);
assert.equal(treeGateBlocker(state.nodes.get('spec:apex')), '');

setNodes([
    {node_key: 'class:spent', tree_type: 'class', point_pool: 'class', row: 1000, points: 34},
    {...apexNode, points: 3},
]);
assert.equal(treeRuleBlocker(state.nodes.get('class:spent'), 1), '');

setNodes([
    {node_key: 'class:spent', tree_type: 'class', point_pool: 'class', row: 1000, points: 34},
    {...apexNode, points: 4},
]);
assert.match(treeRuleBlocker(state.nodes.get('class:spent'), 1), /职业与顶峰天赋合计最多只能投入 38 点/);

setNodes([
    {node_key: 'class:spent', tree_type: 'class', point_pool: 'class', row: 1000, points: 38},
    apexNode,
]);
assert.match(treeRuleBlocker(state.nodes.get('spec:apex'), 1), /职业与顶峰天赋合计最多只能投入 38 点/);

setNodes([
    {node_key: 'spec:spent', tree_type: 'spec', point_pool: 'spec', row: 1000, points: 30},
]);
assert.match(treeRuleBlocker(state.nodes.get('spec:spent'), 1), /专精树最多只能投入 30 点/);

console.log('talent simulator shared class/apex rules: ok');
