/**
 * Regression coverage for FIX-013: the Edit in Combine bridge, executed rather
 * than merely inspected.
 *
 *   node --test tests/edit-roundtrip.test.mjs
 *
 * The bridge lives in one inline <script> with no module boundary, so the
 * helpers under test are lifted out of the page and run here. Everything that
 * needs a DOM or a server is covered by tests/test_edit_roundtrip.py and by the
 * browser run recorded in the audit notes.
 *
 * Zero dependencies: node:test + node:assert only.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const HTML = readFileSync(join(ROOT, 'app', 'static', 'index.html'), 'utf8');

/** Lift one top-level function out of the page so it can actually be run. */
function lift(name) {
    const start = HTML.indexOf(`function ${name}(`);
    assert.notEqual(start, -1, `${name} is missing from the page`);
    const end = HTML.indexOf('\n}', start) + 2;
    return new Function(`${HTML.slice(start, end)}; return ${name};`)();
}

const selectionCount = lift('selectionCount');

test('an empty source selects nothing', () => {
    assert.equal(selectionCount(0, 0, null, 1), 0);
});

test('with no range every frame is selected', () => {
    assert.equal(selectionCount(10, 0, null, 1), 10);
});

test('an inclusive end counts both ends', () => {
    assert.equal(selectionCount(10, 2, 4, 1), 3);
    assert.equal(selectionCount(10, 0, 0, 1), 1);
});

test('an end past the source clamps to what exists', () => {
    assert.equal(selectionCount(10, 8, 500, 1), 2);
});

test('a step counts the frames it lands on', () => {
    assert.equal(selectionCount(10, 0, null, 3), 4);   // 0,3,6,9
    assert.equal(selectionCount(10, 1, 8, 3), 3);      // 1,4,7
});

test('a start beyond the source selects nothing', () => {
    assert.equal(selectionCount(10, 10, null, 1), 0);
    assert.equal(selectionCount(10, 99, 200, 1), 0);
});

test('the count matches the slice the server takes', () => {
    // Mirrors main._select_frames: frames[start:end+1:step]. The browser must
    // agree, because it decides whether to ask about the editor's capacity
    // before the server ever sees the request.
    const serverSlice = (total, start, end, step) => {
        const frames = [...Array(total).keys()];
        return frames.slice(start, end === null ? undefined : end + 1)
                     .filter((_, index) => index % step === 0).length;
    };
    for (const [total, start, end, step] of [
        [10, 0, null, 1], [10, 2, 4, 1], [10, 8, 500, 1],
        [10, 0, null, 3], [10, 1, 8, 3], [118, 0, null, 1], [118, 5, 60, 2],
    ]) {
        assert.equal(selectionCount(total, start, end, step),
                     serverSlice(total, start, end, step),
                     `total=${total} start=${start} end=${end} step=${step}`);
    }
});

test('every transform that changes the picture is reset on import', () => {
    const table = HTML.slice(HTML.indexOf('const IDENTITY_TRANSFORMS = {'));
    const body = table.slice(0, table.indexOf('};') + 2);
    // The frames are already rendered, so anything that would alter them again
    // must be listed here.
    for (const id of ['setting-rotate', 'setting-flip', 'setting-flop',
                      'setting-brightness', 'setting-saturation', 'setting-hue',
                      'setting-grayscale', 'setting-negate', 'setting-sepia',
                      'setting-auto-level', 'setting-normalize', 'setting-sharpen',
                      'setting-blur', 'setting-width', 'setting-height',
                      'setting-resize-mode', 'setting-background',
                      'setting-boomerang']) {
        assert.ok(body.includes(`'${id}'`), `${id} is not reset on import`);
    }
});

test('the identity values really are neutral', () => {
    const table = HTML.slice(HTML.indexOf('const IDENTITY_TRANSFORMS = {'));
    const body = table.slice(0, table.indexOf('};') + 2);
    const identity = new Function(`${body}; return IDENTITY_TRANSFORMS;`)();
    assert.equal(identity['setting-rotate'], '0');
    assert.equal(identity['setting-brightness'], '100');
    assert.equal(identity['setting-saturation'], '100');
    assert.equal(identity['setting-hue'], '100');
    assert.equal(identity['setting-sharpen'], '0');
    assert.equal(identity['setting-blur'], '0');
    assert.equal(identity['setting-width'], '');
    assert.equal(identity['setting-height'], '');
    for (const flag of ['setting-flip', 'setting-flop', 'setting-grayscale',
                        'setting-negate', 'setting-sepia', 'setting-auto-level',
                        'setting-normalize', 'setting-boomerang']) {
        assert.equal(identity[flag], false, flag);
    }
});
