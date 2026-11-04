/** FIX-012: execute the real prefill and submit handler with a small DOM stub.
 * Run: node --test tests/reconvert.test.mjs
 * MAGICFLOW_UI_SOURCE can point at a pre-fix page for the regression check.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(process.env.MAGICFLOW_UI_SOURCE ||
    new URL('../app/static/index.html', import.meta.url), 'utf8');
const baked = {format: 'gif', quality: 72, delay: 150, loop: 3, width: 24, height: 32,
    resize_mode: 'crop', rotate: 90, flip: true, flop: true, boomerang: true,
    brightness: 140, saturation: 0, hue: 50, sharpen: 3, blur: 2,
    grayscale: true, negate: true, sepia: true, auto_level: true, normalize: true,
    background: '#abcdef', strip: true, keep_timing: false, colors: 32, dither: 'None',
    pdf_page_size: 'a4', pdf_orientation: 'landscape', pdf_margin_mm: 20, pdf_fit: 'crop'};
const neutral = {width: '', height: '', resize_mode: 'fit', rotate: '0',
    brightness: '100', saturation: '100', hue: '100', sharpen: '0', blur: '0',
    background: '', flip: false, flop: false, boomerang: false, grayscale: false,
    negate: false, sepia: false, auto_level: false, normalize: false, strip: false,
    keep_timing: true, pdf_page_size: 'original', pdf_orientation: 'portrait',
    pdf_width_mm: '210', pdf_height_mm: '297', pdf_margin_mm: '10', pdf_fit: 'fit'};

function setup() {
    const nodes = new Map();
    const document = {getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, {value: '', checked: false, style: {},
            textContent: '', options: ['gif', 'webp', 'apng', 'mp4', 'webm', 'tiff', 'pdf']
                .map(value => ({value, disabled: false}))});
        return nodes.get(id);
    }};
    const context = vm.createContext({document, FormData, RECONVERT_NUMERIC_FIELDS: [],
        RECIPE_TIMED_FORMATS: ['gif', 'webp', 'apng', 'mp4', 'webm'],
        escapeHtml: s => String(s).replaceAll('<', '&lt;').replaceAll('>', '&gt;')});
    const pdfStart = html.indexOf('const PDF_FIELDS =');
    vm.runInContext(html.slice(pdfStart, html.indexOf('\n', pdfStart)), context);
    for (const name of ['pdfLayoutValues', 'validatePDFLayout', 'appendPDFLayout']) {
        const from = html.indexOf(`function ${name}(`);
        vm.runInContext(html.slice(from, html.indexOf('\n}', from) + 2), context);
    }
    const start = html.indexOf('const RECONVERT_FIELDS = [');
    vm.runInContext(html.slice(start, html.indexOf('\nfunction openReconvert(', start)), context);
    for (const name of ['recipeObject', 'recipeSections', 'recipeSummary',
                        'recipeProvenanceText', 'recipeMetaLine']) {
        const from = html.indexOf(`function ${name}(`);
        if (from >= 0) vm.runInContext(html.slice(from, html.indexOf('\n}', from) + 2), context);
    }
    const field = key => document.getElementById('reconvert-' + key.replaceAll('_', '-'));
    return {context, nodes, document, field};
}

for (const version of ['legacy', 'versioned']) {
    test(`${version} baked transforms never become another job's defaults`, () => {
        const {context, field} = setup();
        const recipe = version === 'legacy' ? baked : {schema_version: 2,
            requested: baked, effective: {format: 'gif', width: 24, height: 32}};
        context.prefillReconvert(recipe);
        for (const [key, value] of Object.entries(neutral))
            assert.equal(typeof value === 'boolean' ? field(key).checked : field(key).value,
                value, key);
        assert.equal(field('quality').value, '72');
        assert.equal(field('loop').value, '3');
        assert.equal(field('dither').value, 'None');
        assert.equal(field('colors').value, '32');
    });
}

test('missing, malformed and future recipes reset stale controls safely', () => {
    const {context, field} = setup();
    for (const recipe of [null, [], 'bad', 5, {quality: 'x', loop: -1, colors: {},
        delay: 99999, dither: 'made-up', rotate: 'sideways', optimize_layers: 'false'},
        {schema_version: 2, requested: [], effective: 'bad'},
        {schema_version: 999, quality: 4, rotate: 90}]) {
        field('rotate').value = '270'; field('negate').checked = true;
        context.prefillReconvert(recipe);
        assert.equal(field('quality').value, '85');
        assert.equal(field('rotate').value, '0');
        assert.equal(field('loop').value, '0');
        assert.equal(field('colors').value, '256');
        assert.equal(field('dither').value, 'Riemersma');
        assert.equal(field('negate').checked, false);
    }
});

test('the unchanged form submits neutral transforms; deliberate new edits still submit', async () => {
    const {context, document, field} = setup();
    const start = html.indexOf("document.getElementById('reconvert-submit').addEventListener");
    const end = html.indexOf('\n});', start) + 4;
    let handler;
    document.getElementById('reconvert-submit').addEventListener = (_, fn) => { handler = fn; };
    const sent = [];
    Object.assign(context, {state: {converting: false}, reconvertTarget: 'baked.gif', pendingReconvertResult: null,
        readNumericField: (id, fallback) => Number(document.getElementById(id).value || fallback),
        blockInvalidFields: () => false, setHeaderConverting: () => {},
        showToast: () => {}, isUneven: () => false, closeReconvert: () => {},
        loadRecentConversions: () => {}, loadSavedFiles: () => {},
        fetch: async (_, opts) => { sent.push(Object.fromEntries(opts.body));
            return {ok: true, json: async () => ({frame_count: 4, output_filename: 'new.gif'})}; }});
    vm.runInContext(html.slice(start, end), context);
    context.prefillReconvert(baked);
    field('format').value = 'gif';
    field('keep').checked = true;
    await handler();
    assert.equal(sent[0].rotate, '0');
    assert.equal(sent[0].negate, 'false');
    assert.equal(sent[0].boomerang, 'false');
    assert.equal(sent[0].width, undefined);
    assert.equal(sent[0].height, undefined);
    assert.equal(sent[0].quality, '72');
    field('rotate').value = '180';
    field('negate').checked = true;
    await handler();
    assert.equal(sent[1].rotate, '180');
    assert.equal(sent[1].negate, 'true');
});

test('provenance separates requested and measured facts, including warnings', () => {
    const {context} = setup();
    const recipe = {schema_version: 2, requested: {...baked, width: 100, height: 80},
        effective: {format: 'webm', width: 24, height: 32, frames: 4,
            encoder: 'libvpx-vp9', video_crf: 32, quality: null,
            timing: {basis: 'frame_intervals', measured_delays_ms: [100, 300, 200],
                encoder_delays_ms: [100, 300, 200, 300]}},
        sources: ['<img onerror=alert(1)>.png'], warnings: ['Final frame duration is unknown.']};
    const summary = context.recipeMetaLine(recipe);
    assert.match(summary, /24×32/);
    assert.match(summary, /libvpx-vp9/);
    assert.match(summary, /CRF 32/);
    assert.doesNotMatch(summary, /150 ms/);
    const history = context.recipeProvenanceText(recipe);
    assert.match(history, /100/);
    assert.match(history, /80/);
    assert.match(history, /Final frame duration is unknown/);
    assert.match(html, /original uploads are not retained/i);
    assert.match(html, /getElementById\('reconvert-provenance'\)\.textContent/);
});
