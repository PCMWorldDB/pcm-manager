const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

function setup(t, saved, filters = 'all') {
    const dom = new JSDOM(`<!doctype html><body>
        <button form="race-selection-form" id="fetch" disabled>Fetch Race Results</button>
        <form id="race-selection-form" data-limit="2" data-storage-key="races:alpha" data-filters="${filters}">
            <input name="race_ids"><span id="race-selection-count"></span>
            <button id="clear-race-selection" type="button" disabled>Deselect all</button>
            <table><thead><tr><th><input type="checkbox" id="select-all-races"></th></tr></thead>
            <tbody>${[1, 2, 3, 4].map((id) => `<tr><td class="race-selection-cell"><input class="race-select" type="checkbox" value="${id}"${id === 4 ? ' disabled' : ''}></td></tr>`).join('')}</tbody></table>
        </form>`, {url: 'https://worlddb.example/n/alpha/races', runScripts: 'outside-only'});
    t.after(() => dom.window.close());
    const {window} = dom;
    if (saved) window.sessionStorage.setItem('races:alpha', JSON.stringify(saved));
    for (const file of ['rider_drag_selection.js', 'race_selection.js']) {
        window.eval(fs.readFileSync(path.join(__dirname, '../../webapp/static/js', file), 'utf8'));
    }
    const find = (selector) => window.document.querySelector(selector);
    const change = (selector, checked = true) => {
        const box = find(selector);
        box.checked = checked;
        box.dispatchEvent(new window.Event('change', {bubbles: true}));
    };
    return {window, find, change};
}

test('select all caps the group size, skips disabled races, and clears selection', (t) => {
    const {find, change} = setup(t);
    assert.equal(find('#fetch').disabled, true);
    change('#select-all-races');
    assert.equal(find('[name="race_ids"]').value, '1,2');
    assert.equal(find('.race-select[value="4"]').checked, false);
    assert.equal(find('#select-all-races').indeterminate, true);
    assert.equal(find('#fetch').disabled, false);
    change('.race-select[value="3"]');
    assert.equal(find('.race-select[value="3"]').checked, false);
    find('#clear-race-selection').click();
    assert.equal(find('[name="race_ids"]').value, '');
    assert.equal(find('#fetch').disabled, true);
});

test('saved selection discards disabled and missing races and resets after filtering', (t) => {
    const saved = {filters: 'all', ids: ['4', '99', '2']};
    const same = setup(t, saved);
    assert.equal(same.find('[name="race_ids"]').value, '2');
    const changed = setup(t, saved, 'new-filter');
    assert.equal(changed.find('[name="race_ids"]').value, '');
});

test('dragging selects a range of eligible race rows', (t) => {
    const {window, find} = setup(t);
    const pointer = (type, properties) => {
        const event = new window.Event(type, {bubbles: true});
        for (const [key, value] of Object.entries(properties)) Object.defineProperty(event, key, {value});
        return event;
    };
    find('.race-selection-cell').dispatchEvent(pointer('pointerdown', {button: 0, pointerId: 1}));
    window.document.elementFromPoint = () => find('.race-select[value="3"]');
    window.document.dispatchEvent(pointer('pointermove', {pointerId: 1, buttons: 1, clientX: 1, clientY: 1}));
    window.document.dispatchEvent(pointer('pointerup', {pointerId: 1}));
    assert.equal(find('[name="race_ids"]').value, '1,2');
    assert.equal(find('#race-selection-count').textContent, '2');
});
