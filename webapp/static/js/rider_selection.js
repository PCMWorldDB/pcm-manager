(() => {
    const form = document.getElementById('rider-selection-form');
    const boxes = [...form.querySelectorAll('.rider-select')];
    const buttons = [...document.querySelectorAll('#rider-selection-form button, button[form="rider-selection-form"]')]
        .filter((button) => button.form === form);
    const clear = document.getElementById('clear-rider-selection');
    const count = document.getElementById('rider-selection-count');
    const payload = form.elements.rider_ids;
    const {storageKey, filters} = form.dataset;
    const maxSelected = Number(form.dataset.limit);
    let selected = new Set();

    try {
        const saved = JSON.parse(sessionStorage.getItem(storageKey));
        if (saved?.filters === filters && Array.isArray(saved.ids)) {
            selected = new Set(saved.ids.slice(0, maxSelected));
        }
    } catch (_) { /* Selection still works on the current page. */ }
    boxes.filter((box) => box.disabled).forEach((box) => selected.delete(box.value));

    function saveSelection() {
        boxes.forEach((box) => {
            box.checked = selected.has(box.value);
            box.closest('tr').classList.toggle('rider-selected', box.checked);
        });
        count.textContent = String(selected.size);
        payload.value = [...selected].join(',');
        buttons.forEach((button) => { button.disabled = selected.size === 0; });
        try {
            sessionStorage.setItem(storageKey, JSON.stringify({filters, ids: [...selected]}));
        } catch (_) { /* Selection still works on the current page. */ }
    }

    form.addEventListener('change', (event) => {
        const box = event.target;
        if (!box.matches('.rider-select') || box.disabled) return;
        if (!box.checked) selected.delete(box.value);
        else if (selected.size < maxSelected) selected.add(box.value);
        saveSelection();
    });
    bindRiderDragSelection(form, {
        cellSelector: '.rider-selection-cell',
        boxes,
        maxSelected,
        getSelection: () => selected,
        setSelection: (ids) => {
            selected = ids;
            saveSelection();
        },
    });
    clear.addEventListener('click', () => {
        selected.clear();
        saveSelection();
    });
    saveSelection();
})();
