/** FIX-024: run the shipped confirmation and delegated deletion handlers.
 * node --test tests/library-actions.test.mjs
 * MAGICFLOW_UI_SOURCE selects the pre-fix snapshot for regression evidence.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, writeFileSync, mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import vm from 'node:vm';

const html = readFileSync(process.env.MAGICFLOW_UI_SOURCE ||
    new URL('../app/static/index.html', import.meta.url), 'utf8');
const entries = [
    ['Recent', 'recent-grid', 'deleteRecent', 'recent'],
    ['My Files', 'saved-files-grid', 'deleteFile', 'saved'],
];
function setup({accepted = true, status = 200, networkError = false,
                selected = [], visible = [], result = {deleted: selected, missing: []}} = {}) {
    const handlers = new Map(), nodes = new Map(), calls = [], prompts = [], errors = [];
    const reloads = {recent: 0, saved: 0};
    const selectedFiles = new Set(selected);
    const document = {
        getElementById(id) {
            if (!nodes.has(id)) nodes.set(id, {addEventListener(type, fn) {
                handlers.set(id + ':' + type, fn);
            }});
            return nodes.get(id);
        },
        querySelectorAll: () => visible.map(name => ({dataset: {select: name}})),
    };
    const context = vm.createContext({document, selectedFiles,
        confirm: message => {prompts.push(message); return accepted;},
        fetch: async (url, options) => {
            calls.push({url, options});
            if (networkError) throw new Error('Network unavailable');
            return {ok: status >= 200 && status < 300, status, json: async () => result};
        },
        loadRecentConversions: () => {reloads.recent++;},
        loadSavedFiles: () => {reloads.saved++;},
        showStatus: (...args) => errors.push(args),
        showToast: (...args) => errors.push(args),
        alert: message => errors.push([message, 'error']),
    });
    const helper = html.match(/^function confirmFileDeletion\([^]*?^\}/m);
    if (helper) vm.runInContext(helper[0], context);
    for (const id of ['recent-grid', 'saved-files-grid', 'delete-selected-btn']) {
        const start = html.indexOf(`document.getElementById('${id}').addEventListener('click'`);
        assert.ok(start >= 0, id + ' handler exists');
        vm.runInContext(html.slice(start, html.indexOf('\n});', start) + 4), context);
    }
    const clickSingle = (id, key, filename) => handlers.get(id + ':click')({target: {
        closest: selector => selector === (key === 'deleteRecent' ? '[data-delete-recent]' : '[data-delete-file]')
            ? {dataset: {[key]: filename}} : null,
    }});
    return {calls, prompts, errors, reloads, selectedFiles, clickSingle,
        clickBulk: () => handlers.get('delete-selected-btn:click')()};
}

for (const [label, id, key, cache] of entries) {
    test(label + ': cancel names the exact file and changes nothing', async () => {
        const app = setup({accepted: false, selected: ['keep.png']});
        const filename = 'holiday "final" & snow.png';
        await app.clickSingle(id, key, filename);
        assert.equal(app.prompts.length, 1);
        assert.ok(app.prompts[0].includes(filename));
        assert.match(app.prompts[0], /cannot be undone/i);
        assert.equal(app.calls.length, 0);
        assert.deepEqual(app.reloads, {recent: 0, saved: 0});
        assert.deepEqual([...app.selectedFiles], ['keep.png']);
        assert.equal(app.errors.length, 0);
    });
    test(label + ': confirmation sends one encoded delete and refreshes on success', async () => {
        const app = setup();
        await app.clickSingle(id, key, 'snow & sun.png');
        assert.equal(app.prompts.length, 1);
        assert.equal(app.calls.length, 1);
        assert.equal(app.calls[0].url, '/files/snow%20%26%20sun.png');
        assert.equal(app.calls[0].options.method, 'DELETE');
        assert.equal(app.reloads[cache], 1);
    });
    for (const failure of [{status: 404}, {status: 403}, {networkError: true}]) {
        test(label + ': failed delete retains the card and can be retried ' + JSON.stringify(failure), async () => {
            const app = setup(failure);
            await app.clickSingle(id, key, 'kept.png');
            assert.equal(app.calls.length, 1);
            assert.deepEqual(app.reloads, {recent: 0, saved: 0});
            assert.equal(app.errors.length, 1);
            assert.equal(app.errors[0][1], 'error');
            await app.clickSingle(id, key, 'kept.png');
            assert.equal(app.calls.length, 2);
            assert.equal(app.prompts.length, 2);
        });
    }
}

test('bulk cancel discloses all selected files and hidden count without changing selection', async () => {
    const app = setup({accepted: false, selected: ['visible.png', 'hidden.png', 'hidden2.png'], visible: ['visible.png', 'unselected.png']});
    await app.clickBulk();
    assert.match(app.prompts[0], /3 files/);
    assert.match(app.prompts[0], /2 selected files.*not shown/i);
    assert.match(app.prompts[0], /also be deleted/i);
    assert.equal(app.calls.length, 0);
    assert.deepEqual([...app.selectedFiles], ['visible.png', 'hidden.png', 'hidden2.png']);
    assert.equal(app.reloads.saved, 0);
});
test('bulk confirmation covers a wholly hidden selection and names a single file', async () => {
    const app = setup({accepted: false, selected: ['hidden.png'], visible: []});
    await app.clickBulk();
    assert.match(app.prompts[0], /hidden\.png/);
    assert.match(app.prompts[0], /1 selected file.*not shown/i);
});
test('bulk success deletes the full confirmed selection and refreshes', async () => {
    const app = setup({selected: ['shown.png', 'hidden.png'], visible: ['shown.png']});
    await app.clickBulk();
    assert.equal(app.calls.length, 1);
    assert.equal(app.calls[0].url, '/files/delete');
    assert.deepEqual(JSON.parse(app.calls[0].options.body), {filenames: ['shown.png', 'hidden.png']});
    assert.equal(app.selectedFiles.size, 0);
    assert.equal(app.reloads.saved, 1);
});
for (const failure of [{status: 403}, {networkError: true}]) {
    test('bulk failure preserves the selection for retry ' + JSON.stringify(failure), async () => {
        const app = setup({...failure, selected: ['kept.png'], visible: ['kept.png']});
        await app.clickBulk();
        assert.deepEqual([...app.selectedFiles], ['kept.png']);
        assert.equal(app.reloads.saved, 0);
        assert.equal(app.errors[0][1], 'error');
        await app.clickBulk();
        assert.equal(app.calls.length, 2);
    });
}
test('bulk partial failure reports errors and keeps failed selections for retry', async () => {
    const app = setup({selected: ['gone.png', 'locked.png'], visible: ['gone.png', 'locked.png'],
        result: {deleted: ['gone.png'], missing: ['locked.png']}});
    await app.clickBulk();
    assert.deepEqual([...app.selectedFiles], ['locked.png']);
    assert.equal(app.errors[0][1], 'error');
    assert.match(app.errors[0][0], /1.*could not be deleted/i);
    assert.equal(app.reloads.saved, 1);
});
test('empty selection performs no confirmation or request', async () => {
    const app = setup(); await app.clickBulk();
    assert.equal(app.prompts.length, 0); assert.equal(app.calls.length, 0);
});

// Real markup, rendering, filtering, delegated icon clicks and refreshes in Chrome.
// Only confirmation answers and the isolated in-memory HTTP backend are mocked.
test('browser: filtered bulk selection, cancellation and retryable failures', () => {
    const work = mkdtempSync(join(tmpdir(), 'magicflow-library-actions-'));
    try {
        const functions = ['confirmFileDeletion', 'renderSavedFiles', 'renderRecentConversions',
            'updateBulkButtons', 'syncFileSelectionUI', 'loadSavedFiles', 'loadRecentConversions',
            'escapeHtml', 'showToast'].map(name => {
                const start = html.search(new RegExp('^(?:async )?function ' + name + '\\(', 'm'));
                return start < 0 ? '' : html.slice(start, html.indexOf('\n}', start) + 2);
            }).join('\n');
        const listeners = [['recent-grid', 'click'], ['saved-files-grid', 'click'],
            ['saved-files-grid', 'change'], ['search-input', 'input'], ['delete-selected-btn', 'click']]
            .map(([id, event]) => {
                const start = html.indexOf(`document.getElementById('${id}').addEventListener('${event}'`);
                return html.slice(start, html.indexOf('\n});', start) + 4);
            }).join('\n');
        const script = `
        const failures=[], check=(ok, message)=>{if(!ok)failures.push(message);};
        const selectedFiles=new Set(), state={savedFiles:[], recentFiles:[]};
        const EXPLODE_EXTS=new Set(), reconvertBlockInfo=()=>({blocked:false,title:''}),
            recipeMetaLine=()=>'', formatBytes=n=>String(n);
        ${html.match(/^const HTML_ESCAPES = .*$/m)?.[0] || ''}
        const files=new Map(['a-shown.png','b-hidden.png'].map(filename=>[filename,{filename,size:12,modified:1}]));
        const prompts=[], requests=[];let accepted=false, status=403, partial=true;
        window.confirm=message=>{prompts.push(message);return accepted;};
        window.fetch=async(url,options={})=>{
            if(url==='/files')return {json:async()=>({files:[...files.values()]})};
            requests.push([url,options]);
            if(status!==200)return {ok:false,status};
            const names=url==='/files/delete'?JSON.parse(options.body).filenames:[decodeURIComponent(url.slice(7))];
            const deleted=names.filter(name=>!partial || name!=='b-hidden.png');
            deleted.forEach(name=>files.delete(name));
            const missing=names.filter(name=>!deleted.includes(name));partial=false;
            return {ok:true,json:async()=>({deleted,missing})};
        };
        ${functions}
        ${listeners}
        const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
        (async()=>{try{
            await loadSavedFiles();await loadRecentConversions();
            const icon=selector=>document.querySelector(selector+' .material-symbols-outlined');
            icon('[data-delete-recent]').click();await tick();
            check(prompts.at(-1).includes('a-shown.png') && requests.length===0,'recent cancel');
            icon('[data-delete-file]').click();await tick();
            check(prompts.at(-1).includes('a-shown.png') && requests.length===0,'library cancel');
            document.querySelectorAll('.file-select').forEach(cb=>{cb.checked=true;cb.dispatchEvent(new Event('change',{bubbles:true}));});
            document.getElementById('view-dashboard').hidden=true;
            const search=document.getElementById('search-input');search.value='shown';search.dispatchEvent(new Event('input'));
            check(document.querySelectorAll('.file-select').length===1 && selectedFiles.size===2,'filter preserves hidden selection');
            const bulk=document.getElementById('delete-selected-btn');bulk.click();await tick();
            check(/2 files/.test(prompts.at(-1)) && /1 selected file.*not shown/.test(prompts.at(-1)),'hidden selection disclosed');
            check(requests.length===0 && selectedFiles.size===2,'bulk cancel changes nothing');
            accepted=true;icon('[data-delete-file]').click();await tick();
            check(document.querySelector('[data-delete-file]') && selectedFiles.size===2,'403 retains card and selection');
            check(document.querySelector('.toast-error')?.textContent.includes('Could not delete'),'visible error');
            status=200;bulk.click();await tick();await tick();
            check(!files.has('a-shown.png') && files.has('b-hidden.png'),'partial backend deletion');
            check(selectedFiles.size===1 && selectedFiles.has('b-hidden.png'),'failed selection retained');
            check(document.querySelector('[data-delete-file]').dataset.deleteFile==='b-hidden.png','failed card retained after refresh');
            check([...document.querySelectorAll('.toast-error')].some(n=>n.textContent.includes('1 could not be deleted')),'partial failure disclosed');
            bulk.click();await tick();await tick();
            check(files.size===0 && selectedFiles.size===0 && !document.querySelector('.file-select'),'retry succeeds');
        }catch(error){failures.push(error.stack || error.message);}
        const result=document.createElement('pre');result.id='library-result';result.textContent=JSON.stringify(failures);document.body.append(result);})();`;
        const fixture = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
            .replace(/<link\b[^>]*>/g, '').replace('</body>', '<script>' + script + '</script></body>');
        const file = join(work, 'fixture.html');writeFileSync(file, fixture);
        const run = spawnSync(process.env.CHROME_BIN || '/usr/bin/google-chrome', [
            '--headless', '--no-sandbox', '--disable-gpu', '--disable-background-networking',
            '--no-first-run', '--virtual-time-budget=3000', '--user-data-dir=' + join(work,'profile'),
            '--dump-dom', 'file://' + file], {encoding:'utf8', timeout:30000, maxBuffer:8*1024*1024});
        assert.equal(run.status, 0, run.stderr);
        const result = run.stdout.match(/<pre id="library-result">([^<]+)<\/pre>/);
        assert.ok(result, 'browser completed the checks');
        assert.deepEqual(JSON.parse(result[1]), []);
    } finally {rmSync(work, {recursive:true, force:true});}
});
