// FIX-014: execute the same geometry preview used before submitting a job.
// node --test --experimental-test-isolation=none tests/geometry.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html = readFileSync(process.env.MAGICFLOW_UI_SOURCE ||
    new URL('../app/static/index.html', import.meta.url), 'utf8');
const context = vm.createContext({});
for (const name of ['_outputDims', 'animationCanvas']) {
    const start = html.indexOf(`function ${name}(`);
    if (start >= 0) vm.runInContext(html.slice(start, html.indexOf('\n}', start) + 2), context);
}

test('automatic canvas uses both maxima, even when no source has that size', () => {
    const dims = [{w: 32, h: 16}, {w: 16, h: 32}, {w: 8, h: 8}];
    assert.deepEqual({...context.animationCanvas(dims, 'apng')}, {w: 32, h: 32});
    assert.deepEqual({...context.animationCanvas(dims, 'gif')}, {w: 32, h: 32});
    assert.equal(context.animationCanvas(dims, 'tiff'), null);
    assert.equal(context.animationCanvas(dims, 'pdf'), null);
});
test('fit preserves small content while exact modes choose the requested box', () => {
    for (const mode of ['fit', 'pad', 'crop', 'stretch']) {
        const dims = [[32,16],[16,32],[8,8]].map(([w,h]) => context._outputDims(w,h,24,24,mode));
        assert.deepEqual({...context.animationCanvas(dims,'apng')}, {w:24,h:24});
        assert.equal(dims[2].w, mode === 'fit' ? 8 : 24);
    }
});
test('MP4 preview includes even padding; width-only fit keeps aspect ratios', () => {
    assert.deepEqual({...context.animationCanvas([{w:31,h:15}], 'mp4')}, {w:32,h:16});
    const dims = [[32,16],[16,32]].map(([w,h]) => context._outputDims(w,h,16,0,'fit'));
    assert.deepEqual({...context.animationCanvas(dims, 'apng')}, {w:16,h:32});
});
test('Match smallest asks for an exact padded canvas and Fit is labelled a bounding box', () => {
    assert.match(html, /resBtn\(small\.w, small\.h, 'pad'/);
    assert.match(html, /Fit — shrink to bounds/);
    assert.doesNotMatch(html, /Mixed sizes — pick one target below so every frame matches/);
});
