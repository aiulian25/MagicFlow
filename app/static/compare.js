/* FEAT-004: two bounded first-frame previews, one shared viewport transform. */
(() => {
    const root = document.getElementById('compare-modal');
    const surfaces = [...root.querySelectorAll('.compare-surface')];
    const slots = [...root.querySelectorAll('.compare-slot')];
    const zoom = document.getElementById('compare-zoom');
    const pairs = new Map(); // Names only; never retain source uploads here.
    let adapters = {}, controller = null, epoch = 0, urls = [], pan = [0, 0], drag = null;
    function reset() { zoom.value = '1'; pan = [0, 0]; draw(); }
    function draw() {
        const width = Math.min(...surfaces.map(el => el.clientWidth || 320));
        const height = Math.min(...surfaces.map(el => el.clientHeight || 240));
        const images = slots.map(slot => slot.querySelector('img'));
        const fit = Math.min(width / Math.max(1, ...images.map(img => img.naturalWidth)),
                             height / Math.max(1, ...images.map(img => img.naturalHeight)));
        images.forEach(img => {
            img.style.width = img.naturalWidth * fit + 'px';
            img.style.height = img.naturalHeight * fit + 'px';
            img.style.transform = `translate(-50%, -50%) translate(${pan[0] * width}px, ${pan[1] * height}px) scale(${zoom.value})`;
        });
        document.getElementById('compare-zoom-value').textContent = Number(zoom.value).toFixed(2) + '×';
    }
    function clear() {
        epoch++;
        if (controller) controller.abort();
        controller = null;
        slots.forEach(slot => {
            const img = slot.querySelector('img');
            img.onload = img.onerror = null; img.removeAttribute('src'); img.hidden = true;
        });
        urls.forEach(url => URL.revokeObjectURL(url)); urls = [];
        drag = null;
    }
    function close() {
        clear(); root.style.display = 'none';
        if (adapters.endModal) adapters.endModal(root);
        if (adapters.afterClose) adapters.afterClose();
    }
    async function load(source, index, ticket, signal) {
        const slot = slots[index], status = slot.querySelector('[data-compare-status]');
        slot.querySelector('h3').textContent = source ? (source.label || source.name || source.file.name) : 'Original unavailable';
        slot.querySelector('[data-compare-info]').textContent = '';
        status.hidden = false; status.textContent = source ? 'Loading preview…' : 'Original no longer available. Select two saved files in My Files to compare instead.';
        if (!source) return;
        try {
            let response;
            if (source.file) {
                const form = new FormData();form.append('file', source.file, source.file.name);
                response = await fetch('/compare-preview', {method: 'POST', body: form, signal});
            } else response = await fetch('/compare-preview/' + encodeURIComponent(source.name), {signal});
            if (!response.ok) {
                let message = response.status === 404 ? 'File is no longer available.' : 'Preview unavailable.';
                try { const error = await response.json();message = error.detail || message; } catch (_) {}
                throw new Error(message);
            }
            const blob = await response.blob();
            if (ticket !== epoch) return;
            if (blob.size > 4 * 640 * 640 + 65536) throw new Error('Preview exceeded its size limit.');
            const value = key => response.headers.get('X-Compare-' + key) || '?';
            slot.querySelector('[data-compare-info]').textContent =
                `Source/frame: ${value('Width')} × ${value('Height')} px · ${value('Bytes')} bytes. Preview: ${value('Preview-Width')} × ${value('Preview-Height')} px.`;
            const url = URL.createObjectURL(blob); urls.push(url);
            const img = slot.querySelector('img');
            img.onload = () => { if (ticket === epoch) {status.hidden = true;img.hidden = false;draw();} };
            img.onerror = () => { if (ticket === epoch) {img.hidden = true;status.hidden = false;status.textContent = 'Browser could not display this preview.';} };
            img.src = url;
        } catch (error) {
            if (ticket !== epoch || signal.aborted) return;
            status.textContent = error.message + ' Close and select two saved files to try another comparison.';
        }
    }
    function open(left, right) {
        clear();controller = new AbortController();const ticket = epoch;
        root.style.display = 'flex';reset();
        if (adapters.beginModal) adapters.beginModal(root, close, document.getElementById('compare-close'));
        Promise.all([load(left, 0, ticket, controller.signal), load(right, 1, ticket, controller.signal)]).then(() => {
            if (ticket === epoch && left && right) {
                // Geometry is stated separately; shared controls do not imply matched timing.
                document.getElementById('compare-geometry').textContent = 'Equal-sized previews align under shared pan/zoom. Different source dimensions are not pixel-aligned.';
            }
        });
    }
    document.getElementById('compare-close').addEventListener('click', close);
    document.getElementById('compare-reset').addEventListener('click', reset);
    zoom.addEventListener('input', draw);
    document.getElementById('compare-background').addEventListener('change', event => {
        const value = ['checker','white','black'].includes(event.target.value) ? event.target.value : 'checker';
        surfaces.forEach(el => {el.dataset.background = value;});
    });
    surfaces.forEach(surface => {
        surface.addEventListener('pointerdown', event => {
            if (event.button !== 0) return;
            drag = [event.pointerId, event.clientX, event.clientY];surface.setPointerCapture(event.pointerId);
        });
        surface.addEventListener('pointermove', event => {
            if (!drag || drag[0] !== event.pointerId) return;
            pan[0] += (event.clientX - drag[1]) / surface.clientWidth;
            pan[1] += (event.clientY - drag[2]) / surface.clientHeight;
            drag = [event.pointerId, event.clientX, event.clientY];draw();
        });
        const stop = () => {drag = null;};
        surface.addEventListener('pointerup', stop);surface.addEventListener('pointercancel', stop);
        surface.addEventListener('lostpointercapture', stop);
        surface.addEventListener('keydown', event => {
            const deltas = {ArrowLeft:[-.05,0],ArrowRight:[.05,0],ArrowUp:[0,-.05],ArrowDown:[0,.05]};
            if (!deltas[event.key]) return;
            event.preventDefault();pan = pan.map((value,index) => value + deltas[event.key][index]);draw();
        });
    });
    window.addEventListener('resize', draw);
    document.addEventListener('click', event => {
        const saved = event.target.closest('[data-compare-saved]');
        const converted = event.target.closest('[data-convert-compare]');
        let pair;
        if (saved) pair = [pairs.get(saved.dataset.compareSaved) || null, {name:saved.dataset.compareSaved, label:'Output · ' + saved.dataset.compareSaved}];
        else if (converted && adapters.converted) pair = adapters.converted(Number(converted.dataset.convertCompare));
        else if (event.target.closest('#compare-result-btn') && adapters.result) pair = adapters.result();
        else if (event.target.closest('#compare-selected-btn') && adapters.selected) pair = adapters.selected();
        else if (event.target.closest('#reconvert-compare') && adapters.reconverted) pair = adapters.reconverted();
        if (pair) open(...pair);
    });
    window.MagicFlowCompare = {open, close, configure: options => {adapters = options;},
        rememberSavedPair(output, source) {
            pairs.delete(output);pairs.set(output, {name:source, label:'Source output · ' + source});
            if (pairs.size > 32) pairs.delete(pairs.keys().next().value);
        }};
})();
