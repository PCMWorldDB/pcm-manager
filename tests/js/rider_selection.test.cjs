const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

function setup(t, {saved, filters = 'unknown'} = {}) {
    const dom = new JSDOM(`<!doctype html><body>
        <form id="identity-check-1"></form>
        <button form="rider-selection-form" id="sync" disabled>Sync</button>
        <form id="rider-selection-form" data-limit="2" data-storage-key="identity:alpha" data-filters="${filters}">
            <input name="rider_ids"><span id="rider-selection-count"></span>
            <button id="clear-rider-selection" type="button" disabled>Deselect all</button>
            <table><tbody>${[1, 2, 3].map((id) => `<tr><td class="rider-selection-cell"><input class="rider-select" type="checkbox" value="${id}"></td></tr>`).join('')}</tbody></table>
            <button form="identity-check-1" id="check">Check identity</button>
        </form>`, {url: 'https://worlddb.example/n/alpha/riders/review', runScripts: 'outside-only'});
    t.after(() => dom.window.close());
    const {window} = dom;
    if (saved) window.sessionStorage.setItem('identity:alpha', JSON.stringify(saved));
    for (const file of ['rider_drag_selection.js', 'rider_selection.js']) {
        window.eval(fs.readFileSync(path.join(__dirname, '../../webapp/static/js', file), 'utf8'));
    }
    const find = (selector) => window.document.querySelector(selector);
    function select(id) {
        const box = find(`.rider-select[value="${id}"]`);
        box.checked = true;
        box.dispatchEvent(new window.Event('change', {bubbles: true}));
    }
    return {window, find, select};
}

test('bulk selection enforces its limit and keeps individual checks available', (t) => {
    const {find, select} = setup(t);
    assert.equal(find('#sync').disabled, true);
    assert.equal(find('#check').disabled, false);
    select(1);
    select(2);
    select(3);
    assert.equal(find('[name="rider_ids"]').value, '1,2');
    assert.equal(find('.rider-select[value="3"]').checked, false);
    assert.equal(find('#sync').disabled, false);
    assert.equal(find('#check').disabled, false);
    find('#clear-rider-selection').click();
    assert.equal(find('[name="rider_ids"]').value, '');
    assert.equal(find('#sync').disabled, true);
    assert.equal(find('#check').disabled, false);
});

test('selection restores across pages but resets when review filters change', (t) => {
    const saved = {filters: 'unknown', ids: ['8', '1']};
    const same = setup(t, {saved});
    assert.equal(same.find('[name="rider_ids"]').value, '8,1');
    assert.equal(same.find('.rider-select[value="1"]').checked, true);
    const changed = setup(t, {saved, filters: 'failed'});
    assert.equal(changed.find('[name="rider_ids"]').value, '');
});
