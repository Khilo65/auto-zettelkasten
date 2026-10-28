import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { JSDOM } from 'jsdom';

test('shipped bundle honors sparse degree caps and opt-in graph-value priority', () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { runScripts: 'dangerously' });
  try {
    const script = dom.window.document.createElement('script');
    script.textContent = readFileSync(new URL('../../src/auto_zettelkasten/web/workspace-vendor.js', import.meta.url), 'utf8');
    dom.window.document.body.append(script);
    const nodes = Array.from({ length: 6 }, (_, i) => ({ id: `n${i}`, path: `n${i}.md` }));
    const edges = [];
    for (let i = 0; i < 6; i++) for (let j = i + 1; j < 6; j++) {
      edges.push({ id: `${i}-${j}`, source: `n${i}`, target: `n${j}`, type: 'semantic_similarity',
        layer: 'discovery', score: 1 - edges.length / 20, graph_value_priority: i === 0 && j === 5 ? 2 : 0 });
    }
    const data = { nodes, edges }, snapshot = JSON.stringify(data);
    const limit = dom.window.WorkspaceKit.limitGraphDiscovery;
    for (const cap of [2, 4]) {
      const projected = limit(data, cap);
      assert.equal(projected.nodes.length, 6);
      assert.ok(projected.nodes.every(node => node.degree <= cap));
      assert.ok(projected.hidden_discovery_edges > 0);
    }
    assert.equal(limit(data).edges.length, 15, 'unchanged default cap8');
    assert.ok(!limit(data, 2).edges.some(edge => edge.id === '0-5'));
    assert.ok(limit(data, 2, { graphValuePriority: true }).edges.some(edge => edge.id === '0-5'));
    assert.equal(JSON.stringify(data), snapshot);
  } finally { dom.window.close(); }
});
