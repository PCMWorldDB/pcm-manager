const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');

const settle = () => new Promise(setImmediate);
function setup(t, responses) {
    const dom = new JSDOM(`<!doctype html><body>
        <form id="add-pcs-race-form" action="/n/alpha/races/pcs/add"><input name="pcs_url" value="https://www.procyclingstats.com/race/test/2026"><button id="get-pcs-race">Get</button></form>
        <div id="pcs-race-progress" hidden><span id="pcs-race-spinner" hidden></span><span id="pcs-race-outcome"></span><span id="pcs-race-message"></span><a id="pcs-race-result-link" hidden>Open race</a></div>
    `, {url: 'https://worlddb.example/n/alpha/races'});
    t.after(() => dom.window.close());
    const calls = [], timers = [], navigations = [];
    const fetch = async (url, options) => {
        calls.push({url, options});
        const data = responses.shift();
        return {ok: data.ok !== false, json: async () => data};
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../webapp/static/js/add_pcs_race.js'), 'utf8'), {
        document: dom.window.document, FormData: dom.window.FormData, fetch,
        window: {setTimeout: (callback) => timers.push(callback), location: {assign: (url) => navigations.push(url)}},
    });
    const find = (selector) => dom.window.document.querySelector(selector);
    const submit = () => find('form').dispatchEvent(new dom.window.Event('submit', {cancelable: true}));
    return {find, calls, timers, navigations, submit};
}

test('existing PCS races navigate immediately without polling', async (t) => {
    const page = setup(t, [{status: 'success', existing: true, race_url: '/n/alpha/races/12'}]);
    page.submit();
    await settle();
    assert.deepEqual(page.navigations, ['/n/alpha/races/12']);
    assert.equal(page.calls.length, 1);
});

test('the spinner remains while queued and success exposes the new race link', async (t) => {
    const page = setup(t, [{status: 'queued', group_id: 1, status_url: '/status/1'},
        {status: 'running'}, {status: 'success', race_url: '/n/alpha/races/pcs/test/2026'}]);
    page.submit();
    assert.equal(page.find('#pcs-race-spinner').hidden, false);
    assert.equal(page.find('#get-pcs-race').disabled, true);
    await settle();
    assert.equal(page.find('#pcs-race-spinner').hidden, false);
    assert.equal(page.timers.length, 1);
    page.timers.shift()();
    await settle();
    assert.equal(page.find('#pcs-race-spinner').hidden, true);
    assert.equal(page.find('#get-pcs-race').disabled, false);
    assert.equal(page.find('#pcs-race-outcome').className, 'text-success');
    assert.equal(page.find('#pcs-race-result-link').hidden, false);
    assert.equal(page.find('#pcs-race-result-link').getAttribute('href'), '/n/alpha/races/pcs/test/2026');
});

test('a fetch failure displays a red cross and error text safely', async (t) => {
    const page = setup(t, [{status: 'queued', group_id: 1, status_url: '/status/1'},
        {status: 'failed', error: '<script>not configured</script>'}]);
    page.submit();
    await settle();
    assert.equal(page.find('#pcs-race-spinner').hidden, true);
    assert.equal(page.find('#pcs-race-outcome').textContent, '✕ ');
    assert.equal(page.find('#pcs-race-outcome').className, 'text-danger');
    assert.equal(page.find('#pcs-race-message').textContent, '<script>not configured</script>');
    assert.equal(page.find('script'), null);
    assert.equal(page.find('#pcs-race-result-link').hidden, true);
    assert.equal(page.find('#get-pcs-race').disabled, false);
});
