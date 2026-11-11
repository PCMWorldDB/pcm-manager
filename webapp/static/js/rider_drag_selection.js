function bindRiderDragSelection(form, {cellSelector, boxes, maxSelected, getSelection, setSelection}) {
    const eligibleBoxes = boxes.filter((box) => !box.disabled);
    const rowBoxes = new Map(eligibleBoxes.map((box, index) => [box.closest('tr'), index]));
    let drag = null;

    function applyRange(end) {
        const next = new Set(drag.base);
        const range = eligibleBoxes.slice(Math.min(drag.start, end), Math.max(drag.start, end) + 1);
        for (const box of range) {
            if (drag.remove) next.delete(box.value);
            else if (next.size < maxSelected) next.add(box.value);
        }
        setSelection(next);
    }

    form.addEventListener('pointerdown', (event) => {
        if (event.button !== 0 || !event.target.closest(cellSelector)) return;
        const start = rowBoxes.get(event.target.closest('tr'));
        if (start === undefined) return;
        event.preventDefault();
        const base = new Set(getSelection());
        drag = {pointerId: event.pointerId, start, end: start, base, remove: base.has(eligibleBoxes[start].value)};
        applyRange(start);
    });
    document.addEventListener('pointermove', (event) => {
        if (!drag || event.pointerId !== drag.pointerId || !(event.buttons & 1)) return;
        const row = document.elementFromPoint(event.clientX, event.clientY)?.closest('tr');
        const end = rowBoxes.get(row);
        if (end === undefined || end === drag.end) return;
        drag.end = end;
        applyRange(end);
    });
    function endDrag(event) {
        if (drag?.pointerId === event.pointerId) drag = null;
    }
    document.addEventListener('pointerup', endDrag);
    document.addEventListener('pointercancel', endDrag);
    window.addEventListener('blur', () => { drag = null; });
}
