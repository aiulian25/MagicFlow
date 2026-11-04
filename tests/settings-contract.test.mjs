/**
 * Regression coverage for FIX-015: a zero the user typed is a zero, a blank
 * field is a default, and a value the server would reject is named before
 * anything is sent.
 *
 *   node --test tests/settings-contract.test.mjs
 *
 * The readers live in one inline <script> with no module boundary, so they are
 * lifted out of the page and run here against a tiny stub of the two DOM calls
 * they make. The full click-through is recorded in the audit notes.
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

/** The page's numeric-field helpers, running against a stub document. */
function loadReaders(values, labels = {}) {
    const slice = (marker, end) => {
        const start = HTML.indexOf(marker);
        assert.notEqual(start, -1, `${marker} is missing from the page`);
        return HTML.slice(start, HTML.indexOf(end, start) + end.length);
    };
    const source = [
        slice('const NUMERIC_FIELDS = {', '};'),
        slice('function fieldSpec(', '\n}'),
        slice('function readNumericField(', '\n}'),
        slice('function firstInvalidField(', '\n}'),
        slice('function fieldLabel(', '\n}'),
    ].join('\n');
    const document = {
        getElementById: id => (id in values ? { value: values[id] } : null),
        querySelector: selector => {
            const id = selector.match(/for="([^"]+)"/)[1];
            return labels[id] ? { textContent: labels[id] } : null;
        },
    };
    return new Function('document', `${source};
        return { NUMERIC_FIELDS, fieldSpec, readNumericField, firstInvalidField, fieldLabel };`)(document);
}

test('a typed zero stays zero', () => {
    // The reproduced defect: `parseInt(value) || 100` turned a deliberate
    // saturation 0 into 100, and the picture came back unchanged.
    const readers = loadReaders({ 'setting-saturation': '0', 'setting-hue': '0' });
    assert.equal(readers.readNumericField('setting-saturation'), 0);
    assert.equal(readers.readNumericField('setting-hue'), 0);
});

test('a blank field means the documented default', () => {
    const readers = loadReaders({
        'setting-saturation': '', 'setting-hue': '   ', 'setting-quality': '',
    });
    assert.equal(readers.readNumericField('setting-saturation'), 100);
    assert.equal(readers.readNumericField('setting-hue'), 100);
    assert.equal(readers.readNumericField('setting-quality'), 85);
});

test('an ordinary value is read as itself', () => {
    const readers = loadReaders({ 'setting-quality': '72', 'setting-sharpen': '1.5' });
    assert.equal(readers.readNumericField('setting-quality'), 72);
    assert.equal(readers.readNumericField('setting-sharpen'), 1.5);
});

test('a decimal control keeps its fraction', () => {
    const readers = loadReaders({ 'setting-blur': '0.25' });
    assert.equal(readers.readNumericField('setting-blur'), 0.25);
});

test('unparseable text falls back rather than becoming NaN', () => {
    const readers = loadReaders({ 'setting-quality': 'abc' });
    assert.equal(readers.readNumericField('setting-quality'), 85);
});

test('both panels read the same setting the same way', () => {
    // The reported inconsistency: identical settings behaved differently in
    // Combine and Re-convert.
    const readers = loadReaders({ 'setting-saturation': '0', 'reconvert-saturation': '0' });
    assert.equal(readers.readNumericField('setting-saturation'),
                 readers.readNumericField('reconvert-saturation'));
});

test('the allowed ranges match the server', () => {
    // Mirrors main._validate_transform_params; drifting from it would either
    // block a legal value or let an illegal one through to a late 400.
    const { NUMERIC_FIELDS } = loadReaders({});
    const expected = {
        delay: [1, 10000], loop: [0, 1000], quality: [1, 100], colors: [2, 256],
        brightness: [50, 150], saturation: [0, 200], hue: [0, 200],
        sharpen: [0, 5], blur: [0, 5], width: [1, 8000], height: [1, 8000],
    };
    for (const [field, [min, max]] of Object.entries(expected)) {
        assert.equal(NUMERIC_FIELDS[field].min, min, `${field} min`);
        assert.equal(NUMERIC_FIELDS[field].max, max, `${field} max`);
    }
});

test('zero is inside the range for the settings that allow it', () => {
    const { NUMERIC_FIELDS } = loadReaders({});
    for (const field of ['saturation', 'hue', 'loop', 'sharpen', 'blur', 'rotate']) {
        assert.equal(NUMERIC_FIELDS[field].min, 0, `${field} should accept 0`);
    }
});

test('an out-of-range value is reported, not silently replaced', () => {
    const readers = loadReaders({ 'setting-quality': '999', 'setting-delay': '100' });
    assert.equal(readers.firstInvalidField(['setting-delay', 'setting-quality']),
                 'setting-quality');
    // It is still read as what was typed, so the message can quote it.
    assert.equal(readers.readNumericField('setting-quality'), 999);
});

test('a blank field is not an invalid field', () => {
    const readers = loadReaders({ 'setting-quality': '', 'setting-hue': '' });
    assert.equal(readers.firstInvalidField(['setting-quality', 'setting-hue']), null);
});

test('the first offending field in panel order is the one reported', () => {
    const readers = loadReaders({
        'setting-delay': '0', 'setting-quality': '999', 'setting-hue': '500',
    });
    assert.equal(
        readers.firstInvalidField(['setting-delay', 'setting-quality', 'setting-hue']),
        'setting-delay');
});

test('values at the edge of a range are accepted', () => {
    const readers = loadReaders({
        'setting-saturation': '0', 'setting-hue': '200',
        'setting-quality': '1', 'setting-colors': '256',
    });
    assert.equal(readers.firstInvalidField(
        ['setting-saturation', 'setting-hue', 'setting-quality', 'setting-colors']), null);
});

test('an error names the control the way the user sees it', () => {
    const readers = loadReaders({ 'setting-quality': '999' }, { 'setting-quality': 'Quality' });
    assert.equal(readers.fieldLabel('setting-quality'), 'Quality');
});

test('a control with no label still gets a usable name', () => {
    const readers = loadReaders({ 'setting-hue': '500' });
    assert.equal(readers.fieldLabel('setting-hue'), 'hue');
});

test('every numeric control Combine sends is validated', () => {
    const list = HTML.slice(HTML.indexOf('const COMBINE_NUMERIC_FIELDS = ['));
    const body = list.slice(0, list.indexOf('];') + 2);
    for (const id of ['setting-delay', 'setting-loop', 'setting-width', 'setting-height',
                      'setting-quality', 'setting-rotate', 'setting-brightness',
                      'setting-saturation', 'setting-hue', 'setting-sharpen',
                      'setting-blur', 'setting-colors']) {
        assert.ok(body.includes(`'${id}'`), `${id} is not validated before submission`);
    }
});

test('no coercing read of a numeric setting is left in the page', () => {
    // `parseInt(...) || default` is the defect itself; it must not come back.
    const coercing = HTML.match(
        /parseInt\(document\.getElementById\('(setting|reconvert|extract|convert)-[a-z]+'\)\.value, 10\) \|\|/g);
    assert.equal(coercing, null, `still coercing: ${coercing}`);
});
