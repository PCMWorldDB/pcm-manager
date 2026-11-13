// Run in a disposable Node container with jsdom installed under /tmp; no app dependency.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const source = (file) => fs.readFileSync(path.join(__dirname, '../../webapp/static/js', file), 'utf8');
const rider = (id, extra = {}) => ({
    rider_id: id, name: `Rider ${id}`, search_name: `Rider ${id}`, search_team: 'Example Team',
    rider_url: `/n/alpha/riders/${id}`, team_url: '/n/alpha/teams/1', team_name: 'Example Team',
    country_id: 1, division_id: 1, nation_name: 'Belgium', stat_mo: id % 100,
    pcs_slug: `rider-${id}`, first_cycling_id: id + 9000, pcs_identity_status: 'unknown',
    monitor_id: null, latest_check_at: null, days_since_last_race: null, ...extra,
});
const json = (value) => JSON.stringify(value).replaceAll('<', '\\u003c');
function setup(t, {riders = [rider(1), rider(2)], namespaceId = 1, saved, limit = 250} = {}) {
    const state = {view: 'stats', page: 1, per_page: 100, q: '', country: '', division: '', team: '',
        stat_key: '', stat_min: '', stat_max: '', identity_flag: '', activity_status: ''};
    const snapshot = {namespace_id: namespaceId, riders, updated_at: '2026-11-13T01:00:00Z'};
    const config = {state, stats: ['mo'], identityLabels: {unknown: 'PCS identity unknown', verified: 'PCS identity verified'},
        namespaceId, storageKey: `riders-table:user:${namespaceId}`, snapshotUrl: '/n/alpha/riders/snapshot'};
    const dom = new JSDOM(`<!doctype html><html><body>
        <form id="riders-filters-form">
            <input name="q" type="search"><input name="team" type="search">
            <select name="country"><option value="">All</option><option value="1">Belgium</option></select>
            <select name="division"><option value="">All</option><option value="1">WT</option></select>
            <select name="stat_key"><option value="">Any stat</option><option value="mo">MO</option></select>
            <input name="stat_min" type="number" min="0" max="99"><input name="stat_max" type="number" min="0" max="99">
            <select name="identity_flag"><option value="">All flags</option><option value="unknown">Unknown</option><option value="verified">Verified</option></select>
            <select name="activity_status"><option value="">All activity</option><option value="recent">Recent</option></select>
            <button id="reset-riders-filters" type="button">Reset</button>
        </form>
        <h5 id="rider-table-title"></h5><span id="rider-result-count">Showing <span id="rider-result-start"></span>–<span id="rider-result-end"></span> of <span id="rider-result-total"></span></span>
        <select id="per_page" form="riders-filters-form" name="per_page"><option value="50">50</option><option value="100" selected>100</option><option value="250">250</option></select>
        <button form="rider-selection-form" id="bulk-action" name="action" value="run_data_sync" disabled>Sync</button>
        <form id="rider-selection-form" data-limit="${limit}">
            <input type="hidden" name="rider_ids"><span id="rider-selection-count"></span>
            <button type="button" id="clear-rider-selection" disabled>Deselect</button>
            <button type="button" id="previous-rider-view">←</button>
            <select id="rider-view"><option value="stats">Stats</option><option value="attributes">Attributes</option><option value="activity">Activity</option><option value="identity">Identity</option></select>
            <button type="button" id="next-rider-view">→</button>
            <button type="button" id="refresh-riders">Refresh</button><span id="rider-snapshot-updated"></span>
            <p id="rider-snapshot-error" hidden></p>
            <table class="riders-table"><colgroup></colgroup><thead></thead><tbody></tbody></table>
        </form>
        <nav id="rider-pagination"></nav><p id="riders-empty" hidden></p>
        <script type="application/json" id="rider-snapshot">${json(snapshot)}</script>
        <script type="application/json" id="rider-table-config">${json(config)}</script>
    </body></html>`, {url: 'https://worlddb.test/n/alpha/riders', runScripts: 'outside-only'});
    t.after(() => dom.window.close());
    const {window} = dom;
    const requests = [];
    window.fetch = async (...args) => {
        requests.push(args);
        return {ok: true, json: async () => ({...snapshot, countries: [], divisions: []})};
    };
    if (saved) window.sessionStorage.setItem(config.storageKey, JSON.stringify(saved));
    window.eval(source('rider_drag_selection.js'));
    window.eval(source('rider_table.js'));
    const find = (selector) => window.document.querySelector(selector);
    const change = (selector, value, event = 'change') => {
        const element = find(selector);
        element.value = value;
        element.dispatchEvent(new window.Event(event, {bubbles: true}));
    };
    const select = (id) => {
        const box = find(`.rider-select[value="${id}"]`);
        box.checked = !box.checked;
        box.dispatchEvent(new window.Event('change', {bubbles: true}));
    };
    return {window, find, change, select, requests, snapshot, config};
}

test('8000 riders render one page; views and filters stay local with stable selection', (t) => {
    const {find, change, select, requests} = setup(t, {riders: Array.from({length: 8000}, (_, i) => rider(i + 1))});
    assert.equal(find('tbody').rows.length, 100);
    assert.equal(find('#rider-result-start').textContent, '1');
    assert.equal(find('#per_page').selectedOptions[0].textContent, '100');
    assert.equal(find('#rider-result-total').textContent, '8000');
    select(1);
    const filters = find('#riders-filters-form');
    change('[name="stat_key"]', 'mo');
    change('[name="stat_min"]', '50', 'input');
    const filterHTML = filters.innerHTML;
    const resultText = find('#rider-result-count').textContent;
    for (const view of ['attributes', 'activity', 'identity', 'stats']) {
        change('#rider-view', view);
        assert.equal(find('#riders-filters-form'), filters);
        assert.equal(filters.innerHTML, filterHTML);
        assert.equal(find('#rider-result-count').textContent, resultText);
        assert.equal(find('[name="rider_ids"]').value, '1');
        assert.equal(find('#rider-selection-count').textContent, '1');
    }
    assert.equal(requests.length, 0);
    find('#reset-riders-filters').click();
    assert.equal(find('.rider-select[value="1"]').checked, true);
    find('[data-page="2"]').click();
    assert.equal(find('tbody').rows.length, 100);
    assert.equal(find('.rider-select').value, '101');
    assert.equal(find('#per_page').selectedOptions[0].textContent, '100');
    assert.equal(find('#rider-result-end').textContent, '200');
    select(101);
    assert.equal(find('[name="rider_ids"]').value, '1,101');
    find('[data-sort="stat_mo"]').click();
    find('[data-sort="stat_mo"]').click();
    assert.equal(find('tbody tr .rider-stat').textContent, '98'); // Sorted globally, still on page 2.
    assert.equal(requests.length, 0);
    change('#per_page', '50');
    assert.equal(find('tbody').rows.length, 50);
    assert.equal(find('#rider-result-start').textContent, '1');
    assert.equal(find('#per_page').selectedOptions[0].textContent, '50');
    assert.equal(requests.length, 0);
});

test('selection cap, deleted riders, action enablement and refreshed drag targets', (t) => {
    const {find, select, change, window} = setup(t, {limit: 2,
        riders: [rider(1), rider(2), rider(3), rider(4, {is_deleted: true})]});
    assert.equal(find('#bulk-action').disabled, true);
    assert.equal(find('.rider-select[value="4"]').disabled, true);
    select(1); select(2); select(3);
    assert.equal(find('[name="rider_ids"]').value, '1,2');
    assert.equal(find('.rider-select[value="3"]').checked, false);
    assert.equal(find('#bulk-action').disabled, false);
    find('#clear-rider-selection').click();
    change('#rider-view', 'attributes');
    const firstCell = find('tbody .rider-selection-cell');
    firstCell.dispatchEvent(new window.MouseEvent('pointerdown', {bubbles: true, button: 0, buttons: 1}));
    assert.equal(find('[name="rider_ids"]').value, '1');
    window.document.elementFromPoint = () => find('.rider-select[value="3"]');
    window.document.dispatchEvent(new window.MouseEvent('pointermove', {bubbles: true, buttons: 1}));
    assert.equal(find('[name="rider_ids"]').value, '1,2');
    window.document.dispatchEvent(new window.MouseEvent('pointerup', {bubbles: true}));
});

test('refresh preserves controls and prunes unavailable selections; failures keep usable data', async (t) => {
    const {find, select, change, window, snapshot, config} = setup(t);
    select(1); change('#rider-view', 'attributes');
    window.fetch = async () => ({ok: true, json: async () => ({...snapshot,
        riders: [rider(2, {stat_mo: 88})], countries: [{value: '1', label: 'Belgium'}], divisions: []})});
    find('#refresh-riders').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(find('#rider-view').value, 'attributes');
    assert.equal(find('[name="rider_ids"]').value, '');
    assert.equal(find('tbody').rows.length, 1);
    window.fetch = async () => { throw new Error('offline'); };
    find('#refresh-riders').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(find('#rider-snapshot-error').hidden, false);
    assert.equal(find('tbody').rows.length, 1);
    assert.equal(find('#refresh-riders').disabled, false);
    window.fetch = async () => ({ok: true, json: async () => ({...snapshot, namespace_id: config.namespaceId + 1})});
    find('#refresh-riders').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(find('#rider-snapshot-error').hidden, false);
    assert.equal(find('tbody').rows.length, 1);
});

test('accent matching, combined filters, empty results, safe HTML and zero-valued stats', (t) => {
    const {find, change, window} = setup(t, {riders: [
        rider(1, {name: '<img src=x onerror=alert(1)>', search_name: 'Łukasz Dvořák', stat_mo: 0}),
        rider(2, {stat_mo: 80, pcs_identity_status: 'verified', monitor_id: 2, days_since_last_race: 10}),
    ]});
    assert.equal(find('tbody img'), null);
    assert.match(find('tbody').textContent, /<img src=x/);
    change('[name="q"]', 'lukasz dvorak', 'input');
    assert.equal(find('tbody').rows.length, 1);
    assert.equal(find('.rider-stat').textContent, '0');
    change('[name="q"]', 'missing', 'input');
    assert.equal(find('tbody').rows.length, 0);
    assert.equal(find('#riders-empty').hidden, false);
    find('#reset-riders-filters').click();
    change('[name="stat_key"]', 'mo'); change('[name="stat_min"]', '75', 'input');
    change('[name="identity_flag"]', 'verified'); change('[name="activity_status"]', 'recent');
    change('#rider-view', 'attributes');
    assert.equal(find('tbody').rows.length, 1);
    assert.equal(find('.rider-select').value, '2');
    assert.equal(window.sessionStorage.getItem('riders-table:user:2'), null);
});

test('saved state restores controls, paging and columns without persisting the roster', (t) => {
    const {find, window, config} = setup(t, {riders: Array.from({length: 300}, (_, i) => rider(i + 1)),
        saved: {view: 'identity', page: 2, per_page: 50, q: 'Rider'}});
    assert.equal(find('#rider-view').value, 'identity');
    assert.equal(find('#per_page').value, '50');
    assert.equal(find('[name="q"]').value, 'Rider');
    assert.equal(find('tbody').rows.length, 50);
    assert.equal(find('.rider-select').value, '51');
    assert.equal(JSON.parse(window.sessionStorage.getItem(config.storageKey)).riders, undefined);
});

test('saved filters with removed country options reset to the visible control value', (t) => {
    const {find} = setup(t, {saved: {country: '999'}});
    assert.equal(find('[name="country"]').value, '');
    assert.equal(find('tbody').rows.length, 2);
});

test('view arrows wrap through available views and preserve filters and selection without requests', (t) => {
    const {find, select, change, requests} = setup(t);
    select(1);
    change('[name="q"]', 'Rider', 'input');
    const filters = find('#riders-filters-form').innerHTML;
    find('#previous-rider-view').click();
    assert.equal(find('#rider-view').value, 'identity');
    assert.ok(find('[data-sort="pcs_slug"]'));
    find('#next-rider-view').click();
    assert.equal(find('#rider-view').value, 'stats');
    assert.ok(find('[data-sort="stat_mo"]'));
    find('#next-rider-view').click();
    assert.equal(find('#rider-view').value, 'attributes');
    assert.deepEqual([...find('thead').querySelectorAll('th')].map((heading) => heading.textContent),
        ['Select', 'Name', 'Team', 'Country', 'Birth Date', 'Height', 'Weight']);
    assert.equal(find('[name="rider_ids"]').value, '1');
    assert.equal(find('[name="q"]').value, 'Rider');
    assert.equal(find('#riders-filters-form').innerHTML, filters);
    assert.equal(requests.length, 0);
});
