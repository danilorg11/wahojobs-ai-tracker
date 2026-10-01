// Shipped candidate client, synthetic DOM/network; no real browser/provider.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const clientPath = process.env.WAHOJOBS_CANDIDATE_CLIENT_SCRIPT || path.resolve(__dirname, '../../../website/public/candidate-client.js');
const script = fs.readFileSync(clientPath, 'utf8');
const target = '/jobs/opportunity-9002?variant=9003&return_to=%2Fjobs%3Flocation%3DBrazil%26page%3D2';
const settled = () => new Promise(resolve => setTimeout(resolve, 15));

function fixture(initial = null, options = {}) {
    const dom = new JSDOM(`<article><h2>Opportunity</h2><a href="https://apply.example.test/9003" data-apply>Apply with employer</a><div class="candidate-tracking" data-candidate-canonical="9002" data-candidate-variant="${options.variant || '9003'}" data-candidate-return="${target.replace(/&/g, '&amp;')}"><button data-candidate-action="save">Save</button><button data-candidate-action="applied">Mark as applied</button><button data-candidate-action="not_interested">Not interested</button><span role="status"></span><a href="/my-jobs">My Jobs</a></div></article>`, {
        url: 'https://www.wahojobs.com/jobs?location=Brazil&page=2', runScripts: 'outside-only'
    });
    const h = { dom, writes: [], reads: [], state: initial, authenticated: options.authenticated !== false, navigations: [], failWrites: false, failState: false };
    const location = { origin: 'https://www.wahojobs.com', assign: url => h.navigations.push(url) };
    dom.window.fetch = async (url, request = {}) => {
        if (!request.method) {
            h.reads.push(url);
            if (h.failState) return { ok: false };
            return { ok: true, json: async () => ({ authenticated: h.authenticated, csrf: 'c'.repeat(43), states: h.state ? { '9002': h.state } : {} }) };
        }
        const form = Object.fromEntries(new URLSearchParams(request.body));
        h.writes.push({ url, request, form });
        if (h.hold) await h.hold;
        if (h.failWrites) throw Error('Synthetic network failure');
        if (!h.authenticated) return { ok: true, url: 'https://www.wahojobs.com/candidate/login' };
        h.state ||= { workflow_status: 'recommended', visibility: 'visible', version: 0, selected_variant: 9003 };
        const action = form.action;
        if (action === 'save') h.state.workflow_status = 'saved';
        if (action === 'applied') { h.state.workflow_status = 'applied'; h.state.restorable_applied = true; }
        if (action === 'undo_applied') h.state.workflow_status = 'saved';
        if (action === 'unsave') h.state.workflow_status = 'recommended';
        if (action === 'not_interested') { h.state.visibility = 'hidden'; h.state.undo_available = true; }
        if (action === 'show_again' || action === 'undo_discovery') h.state.visibility = 'visible';
        h.state.version++;
        return { ok: true, url: 'https://www.wahojobs.com' + form.return_to };
    };
    h.start = () => dom.window.Function('location', script)(location);
    h.button = action => dom.window.document.querySelector(`button[data-candidate-action="${action}"]`);
    h.click = async action => { h.button(action).click(); await settled(); };
    h.feedback = () => dom.window.document.querySelector('.candidate-tracking [role="status"]').textContent;
    return h;
}

test('anonymous explicit Save sends exact safe intent then navigates to candidate login', async () => {
    const h = fixture(null, { authenticated: false });
    try {
        h.start(); await settled();
        assert.equal(h.writes.length, 0);
        await h.click('save');
        assert.equal(h.writes.length, 1);
        assert.equal(h.writes[0].form.return_to, target);
        assert.equal(h.writes[0].form.variant, '9003');
        assert.equal(h.writes[0].form.canonical, '9002');
        assert.equal(h.writes[0].form.version, '0');
        assert.match(h.writes[0].form.key, /^[A-Za-z0-9_-]{43}$/);
        assert.deepEqual(h.navigations, ['https://www.wahojobs.com/candidate/login']);
        assert.deepEqual(h.reads, ['/candidate/state?ids=9002']);
    } finally { h.dom.window.close(); }
});

test('Save Applied correction hide and restore reconcile confirmed status without touching employer Apply', async () => {
    const h = fixture();
    try {
        h.start(); await settled();
        const apply = h.dom.window.document.querySelector('[data-apply]');
        assert.equal(apply.href, 'https://apply.example.test/9003');
        assert.equal(h.writes.length, 0);
        for (const [action, status] of [['save', 'saved'], ['applied', 'applied'], ['undo_applied', 'saved']]) {
            await h.click(action);
            assert.equal(h.state.workflow_status, status);
        }
        await h.click('not_interested');
        assert.equal(h.dom.window.document.querySelector('article').hidden, true);
        assert.ok(h.dom.window.document.querySelector('.candidate-hidden-notice'));
        await h.click('show_again');
        assert.equal(h.dom.window.document.querySelector('article').hidden, false);
        assert.equal(h.state.workflow_status, 'saved');
        assert.equal(h.dom.window.document.querySelector('.candidate-hidden-notice'), null);
        assert.equal(h.writes.length, 5);
    } finally { h.dom.window.close(); }
});

test('a hidden canonical sibling hides the representative and restores the persisted selected variant', async () => {
    const h = fixture({ workflow_status: 'saved', visibility: 'hidden', version: 3, selected_variant: 9003, undo_available: true }, { variant: 9004 });
    try {
        h.start(); await settled();
        assert.equal(h.dom.window.document.querySelector('article').hidden, true);
        assert.ok(h.button('show_again'));
        await h.click('show_again');
        assert.equal(h.writes[0].form.variant, '9003');
        assert.equal(h.dom.window.document.querySelector('article').hidden, false);
        assert.match(h.feedback(), /Saved.*another source version/);
        assert.equal(h.button('save'), null);
        assert.equal(h.button('applied'), null);
    } finally { h.dom.window.close(); }
});

test('failed writes never look saved and retries retain the idempotency key', async () => {
    const h = fixture();
    try {
        h.start(); await settled();
        h.failWrites = true;
        await h.click('save');
        assert.match(h.feedback(), /not confirmed/);
        assert.equal(h.state, null);
        const key = h.writes[0].form.key;
        h.failWrites = false;
        await h.click('save');
        assert.equal(h.writes[1].form.key, key);
        assert.equal(h.state.workflow_status, 'saved');
        assert.equal(h.feedback(), 'Saved');
    } finally { h.dom.window.close(); }
});

test('duplicate clicks issue only one in-flight tracking operation', async () => {
    const h = fixture();
    try {
        h.start(); await settled();
        let release;
        h.hold = new Promise(resolve => { release = resolve; });
        const button = h.button('save');
        button.click(); button.dispatchEvent(new h.dom.window.Event('click'));
        await settled();
        assert.equal(h.writes.length, 1);
        release(); await settled();
        assert.equal(h.state.workflow_status, 'saved');
    } finally { h.dom.window.close(); }
});

test('authentication state outage leaves anonymous Apply usable with accurate failure feedback', async () => {
    const h = fixture();
    try {
        h.failState = true;
        h.start(); await settled();
        assert.match(h.feedback(), /Tracking temporarily unavailable/);
        assert.equal(h.button('save').disabled, true);
        assert.equal(h.dom.window.document.querySelector('[data-apply]').href, 'https://apply.example.test/9003');
        assert.equal(h.writes.length, 0);
    } finally { h.dom.window.close(); }
});

test('server-emitted continuation performs a CSRF POST; a bare catalog query performs no write', () => {
    const dom = new JSDOM('<form data-candidate-resume method="post" action="/candidate/resume"><input name="csrf" value="synthetic"></form>', { url: 'https://www.wahojobs.com/candidate/resume', runScripts: 'outside-only' });
    let submitted = 0;
    const form = dom.window.document.querySelector('form');
    form.requestSubmit = () => { submitted++; assert.equal(form.method, 'post'); assert.equal(form.elements.csrf.value, 'synthetic'); };
    dom.window.eval(script);
    assert.equal(submitted, 1);
    dom.window.close();
});
