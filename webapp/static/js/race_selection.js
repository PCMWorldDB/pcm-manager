(() => {
    const form = document.getElementById('race-selection-form');
    if (!form) return;
    const boxes = [...form.querySelectorAll('.race-select')].filter((box) => !box.disabled);
    const eligible = new Set(boxes.map((box) => box.value));
    const buttons = [...document.querySelectorAll('button[form="race-selection-form"]')];
    const clear = document.getElementById('clear-race-selection');
    const all = document.getElementById('select-all-races');
    const count = document.getElementById('race-selection-count');
    const {storageKey, filters} = form.dataset;
    const limit = Number(form.dataset.limit);
    let selected = new Set();
    try {
        const saved = JSON.parse(sessionStorage.getItem(storageKey));
        if (saved?.filters === filters && Array.isArray(saved.ids)) {
            selected = new Set(saved.ids.filter((id) => eligible.has(id)).slice(0, limit));
        }
    } catch (_) { /* Selection works without storage. */ }

    function update() {
        boxes.forEach((box) => {
            box.checked = selected.has(box.value);
            box.closest('tr').classList.toggle('race-selected', box.checked);
        });
        form.elements.race_ids.value = [...selected].join(',');
        count.textContent = String(selected.size);
        buttons.forEach((button) => { button.disabled = selected.size === 0; });
        clear.disabled = selected.size === 0;
        all.disabled = boxes.length === 0;
        all.checked = boxes.length > 0 && selected.size === boxes.length;
        all.indeterminate = selected.size > 0 && selected.size < boxes.length;
        try {
            sessionStorage.setItem(storageKey, JSON.stringify({filters, ids: [...selected]}));
        } catch (_) { /* Selection works without storage. */ }
    }
    form.addEventListener('change', (event) => {
        const box = event.target;
        if (!box.matches('.race-select') || box.disabled) return;
        if (!box.checked) selected.delete(box.value);
        else if (selected.size < limit) selected.add(box.value);
        update();
    });
    all.addEventListener('change', () => {
        const ordered = [...form.querySelectorAll('.race-select')].filter((box) => !box.disabled);
        selected = all.checked ? new Set(ordered.slice(0, limit).map((box) => box.value)) : new Set();
        update();
    });
    clear.addEventListener('click', () => { selected.clear(); update(); });
    bindRiderDragSelection(form, {
        cellSelector: '.race-selection-cell', boxes: () => [...form.querySelectorAll('.race-select')],
        maxSelected: limit, getSelection: () => selected,
        setSelection: (ids) => { selected = ids; update(); },
    });
    update();
})();
