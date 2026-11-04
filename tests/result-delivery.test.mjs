/**
 * Regression coverage for FIX-016: every completed job reaches the user through
 * one delivery path, and a temporary result is always both reachable and
 * acknowledged.
 *
 *   node --test tests/result-delivery.test.mjs
 *
 * The UI is one inline <script> in index.html with no module boundary, so these
 * are structural guards over that source — the same approach as
 * tests/filename-escaping.test.mjs. They catch the defect class the audit found:
 * a completion path that quietly drops `delete_after_download` and leaves the
 * result unreachable or retained. Behaviour itself is verified in the browser.
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
const SCRIPT = HTML.slice(HTML.indexOf('<script>'), HTML.lastIndexOf('</script>'));

/** The three server responses that carry a result the user must be able to collect. */
const COMPLETION_HANDLERS = [
    { name: 'Combine', marker: 'showDownloadArea(result.output_filename' },
    { name: 'Convert All', marker: 'item.temporary = result.delete_after_download' },
    { name: 'Re-convert', marker: 'if (result.delete_after_download) {' },
];

test('every completion path reads the result lifecycle instead of discarding it', () => {
    for (const { name, marker } of COMPLETION_HANDLERS) {
        assert.ok(SCRIPT.includes(marker),
            `${name} no longer reads delete_after_download from its response`);
    }
});

test('the temporary acknowledgement has exactly one implementation', () => {
    const definition = SCRIPT.match(/function releaseTempOutput\(/g) || [];
    assert.equal(definition.length, 1, 'releaseTempOutput must be defined once');

    // Exactly two acknowledgement sites: deliverResult, which covers every
    // single-result path, and the bulk archive, whose one response carries many
    // results and so cannot go through deliverResult.
    const callers = SCRIPT.match(/releaseTempOutput\(/g) || [];
    assert.equal(callers.length, 3,
        `definition + deliverResult + bulk archive, found ${callers.length}`);
});

test('handing a produced result to the user happens in exactly two places', () => {
    // Saving bytes outside these two is how a batch result used to be delivered
    // with no acknowledgement at all.
    assert.equal((SCRIPT.match(/function saveBlob\(/g) || []).length, 1);
    const callers = SCRIPT.match(/saveBlob\(/g) || [];
    assert.equal(callers.length, 3, 'definition + deliverResult + bulk archive');
    for (const owner of ['async function deliverResult', 'async function downloadAllConverted']) {
        const body = SCRIPT.slice(SCRIPT.indexOf(owner));
        assert.ok(body.slice(0, body.indexOf('\n}') + 2).includes('saveBlob('),
            `${owner} must be one of the two delivery points`);
    }
});

test('a batch card offers delivery, not a bare link to the file', () => {
    // The reproduced defect: `<a href="/download/…">` collected the bytes while
    // the server kept the temporary until the hourly sweep.
    const renderer = SCRIPT.slice(SCRIPT.indexOf('function renderConvertCards'),
                                  SCRIPT.indexOf('grid.innerHTML = html'));
    assert.ok(renderer.includes('data-convert-download='), 'cards need a delivery control');
    assert.ok(!renderer.includes('href="/download/'),
        'a batch card must not link straight at the output');
});

test('a result is only acknowledged after the bytes are handed over', () => {
    const body = SCRIPT.slice(SCRIPT.indexOf('async function deliverResult'));
    const deliverer = body.slice(0, body.indexOf('\n}') + 2);
    assert.ok(deliverer.indexOf('saveBlob(') < deliverer.indexOf('releaseTempOutput('),
        'releaseTempOutput must come after the blob is saved, never before');
    assert.ok(/if \(!res\.ok\) throw/.test(deliverer),
        'a failed download must throw so the caller keeps its retry');
});

test('a temporary re-conversion is offered instead of being closed over', () => {
    assert.ok(SCRIPT.includes('function showReconvertResult('),
        'the re-convert modal must be able to present a temporary result');
    assert.ok(/id="reconvert-download"/.test(HTML),
        'the re-convert result needs its own download control');
    // The old defect: success always closed the modal, orphaning the result.
    const handler = SCRIPT.slice(SCRIPT.indexOf("document.getElementById('reconvert-submit').addEventListener"));
    // The handler's own catch, not the inline `catch (_)` of a detail parse.
    const successBranch = handler.slice(0, handler.indexOf('    } catch (err) {'));
    const closeIndex = successBranch.indexOf('closeReconvert();');
    const guardIndex = successBranch.indexOf('if (result.delete_after_download)');
    assert.ok(guardIndex !== -1 && guardIndex < closeIndex,
        'closeReconvert() must be reachable only for a kept result');
});

test('bulk download asks for one archive rather than a download per file', () => {
    const fn = SCRIPT.slice(SCRIPT.indexOf('async function downloadAllConverted'));
    const body = fn.slice(0, fn.indexOf('\n}') + 2);
    assert.ok(body.includes("fetch('/files/archive'"), 'must use the archive endpoint');
    assert.ok(!/setTimeout\(r, \d+\)/.test(body),
        'the staggered per-file download loop must be gone');
});

test('bulk acknowledgement is limited to temporary outputs', () => {
    const fn = SCRIPT.slice(SCRIPT.indexOf('async function downloadAllConverted'));
    const body = fn.slice(0, fn.indexOf('\n}') + 2);
    assert.ok(/\.filter\(f => f\.temporary\)[\s\S]{0,120}releaseTempOutput/.test(body),
        'kept outputs must never be released after a bulk download');
});

test('a new result resets the download offer', () => {
    const fn = SCRIPT.slice(SCRIPT.indexOf('function showDownloadArea'));
    const body = fn.slice(0, fn.indexOf('\n}') + 2);
    assert.ok(body.includes('btn.innerHTML = DOWNLOAD_LABEL'),
        'the button must not keep reading "Downloaded" for the next result');
    assert.ok(body.includes('btn.disabled = false'), 'and must be clickable again');
});

test('preview fallback is shared by the completion area and the lightbox', () => {
    const definitions = SCRIPT.match(/function attachPreviewFallback\(/g) || [];
    assert.equal(definitions.length, 1);
    const callers = SCRIPT.match(/attachPreviewFallback\(/g) || [];
    assert.equal(callers.length, 3, 'one definition plus the two preview surfaces');
    const fallback = SCRIPT.slice(SCRIPT.indexOf('function attachPreviewFallback'));
    assert.ok(fallback.slice(0, 500).includes("'/thumb/'"),
        'an undecodable original must fall back to the server thumbnail first');
});

test('an exhausted preview explains itself instead of showing an empty frame', () => {
    assert.ok(/id="preview-unavailable"/.test(HTML));
    const fn = SCRIPT.slice(SCRIPT.indexOf('function showDownloadArea'));
    const body = fn.slice(0, fn.indexOf('\n}') + 2);
    assert.ok(body.includes("getElementById('preview-unavailable')"),
        'showDownloadArea must drive the fallback message');
});

test('the temporary note states expiry as well as deletion on download', () => {
    const fn = SCRIPT.slice(SCRIPT.indexOf('function temporaryResultNote'));
    const body = fn.slice(0, fn.indexOf('\n}') + 2);
    assert.ok(body.includes('expiresIn'), 'the note must use the server-sent expiry');
    assert.ok(/removed once you download it/.test(body) && /if you don/.test(body),
        'both halves of the lifecycle must be stated');
});
