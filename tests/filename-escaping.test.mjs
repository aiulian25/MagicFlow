// Regression coverage for filename attribute injection in the single-file UI.
// Run from the repository root (Node 18+, no dependencies):
//   node --test tests/filename-escaping.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const INDEX_HTML = readFileSync(new URL('../app/static/index.html', import.meta.url), 'utf8');

// Names a user can pick locally, or that can already sit in a mounted output dir.
const HOSTILE_NAMES = [
    `x" onload="document.title='AUDIT_FILENAME_EXECUTED'" y.png`,  // the reproduced payload
    `x' onerror='alert(1)' y.png`,
    `<img src=x onerror=alert(1)>.gif`,
    `both"quotes'and<angle>&amp;ampersand.webp`,
];
const ORDINARY_NAMES = ['holiday_2026.png', 'Zoe Doll 7.jpeg', 'résumé–final (2).tiff'];

const ENTITY_DECODES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
const decodeEntities = text => text.replace(/&(?:amp|lt|gt|quot|#39);/g, entity => ENTITY_DECODES[entity]);

// Evaluate the shipped escapeHtml (and its lookup table), not a copy. The
// stand-in document performs HTML-spec text serialization (&, U+00A0, <, >),
// which is exactly what textContent→innerHTML returns, so a DOM-based escaper
// is judged on its output instead of crashing.
function loadEscapeHtml() {
    const table = INDEX_HTML.match(/^const HTML_ESCAPES = .*$/m);
    const source = INDEX_HTML.match(/^function escapeHtml\(str\) \{[\s\S]*?^\}$/m);
    assert.ok(source, 'escapeHtml() not found in app/static/index.html');
    const document = {
        createElement: () => {
            let text = '';
            return {
                set textContent(value) { text = value; },
                get innerHTML() {
                    return text.replace(/&/g, '&amp;').replace(/ /g, '&nbsp;')
                        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
                },
            };
        },
    };
    const context = { document };
    vm.runInNewContext(`${table ? table[0] : ''}\n${source[0]}`, context);
    return context.escapeHtml;
}

const escapeHtml = loadEscapeHtml();

test('escaped filenames contain nothing that can end a quoted attribute or open a tag', () => {
    for (const name of HOSTILE_NAMES) {
        const escaped = escapeHtml(name);
        assert.doesNotMatch(escaped, /["'<>]/, `raw delimiter survives in ${JSON.stringify(escaped)}`);
        assert.doesNotMatch(escaped, /&(?!(?:amp|lt|gt|quot|#39);)/, `bare & in ${JSON.stringify(escaped)}`);
    }
});

test('escaping round-trips exactly, so title, alt and dataset read back the real filename', () => {
    for (const name of [...HOSTILE_NAMES, ...ORDINARY_NAMES]) {
        assert.equal(decodeEntities(escapeHtml(name)), name);
    }
    for (const name of ORDINARY_NAMES) {
        assert.equal(escapeHtml(name), name, 'ordinary filenames must render unchanged');
    }
});

// Filename sources whose raw value must never be interpolated into an attribute:
// File#name, saved-file .filename / .resultName, and `filename` parameters.
const FILENAME_SOURCE = /\.(?:name|filename|resultName)\b|\bfilename\b/;
const SAFE_WRAPPER = /^\s*(?:escapeHtml|encodeURIComponent)\(/;

// Scope: filename sources only. Other user text (e.g. preset names) is kept out
// of attributes by its renderer and is not tracked here.
test('every filename reaching an HTML attribute goes through escapeHtml or encodeURIComponent', () => {
    // `name` locals bound from a filename feed most card attributes, so each must hold an escaped value.
    const filenameBindings = [...INDEX_HTML.matchAll(/\bconst name\s*=\s*([^;]+);/g)]
        .map(match => match[1])
        .filter(assignment => FILENAME_SOURCE.test(assignment));
    assert.ok(filenameBindings.length > 0, 'expected card templates to bind an escaped `name`');
    for (const assignment of filenameBindings) {
        assert.match(assignment, SAFE_WRAPPER, `const name = ${assignment}`);
    }

    const templateInterpolations = [...INDEX_HTML.matchAll(/\b[a-z-]+="[^"]*"/gi)]
        .flatMap(attribute => [...attribute[0].matchAll(/\$\{([^}]*)\}/g)].map(match => match[1]));
    const concatenatedInterpolations = [...INDEX_HTML.matchAll(/\b[a-z-]+="' *\+ *([^+]+?) *\+ *'"/gi)]
        .map(match => match[1]);
    const unescaped = [...templateInterpolations, ...concatenatedInterpolations].filter(expression =>
        FILENAME_SOURCE.test(expression) && !SAFE_WRAPPER.test(expression));
    assert.deepEqual(unescaped, [], 'raw filename interpolated into an HTML attribute');
});
