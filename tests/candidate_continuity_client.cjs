/* Execute the shipped inline script and generated DOM against real HTTPS handlers.
 * Python supplies only transport/cookies and read-only persistence observations.
 * No response or positive form field is fabricated. No resources are fetched by jsdom.
 */
'use strict';
const assert = require('node:assert/strict');
const readline = require('node:readline');
const crypto = require('node:crypto');
const {JSDOM, VirtualConsole} = require('jsdom');
const origin = process.argv[2], mode = process.argv[3];
assert.equal(new URL(origin).hostname, 'localhost');
assert.notEqual(new URL(origin).port, '8802');
let waiting;
readline.createInterface({input: process.stdin}).on('line', line => {
  const result = JSON.parse(line);
  const resolve = waiting; waiting = null; resolve(result);
});
function rpc(request) {
  assert.ok(waiting == null, 'One transport request at a time');
  return new Promise(resolve => { waiting = resolve; process.stdout.write(JSON.stringify(request)+'\n'); });
}
const observations = [], requests = [], scriptHashes = new Set(), clientErrors = [];
let dom, pending = 0;
async function transport(target, options = {}) {
  const url = new URL(target, origin);
  assert.equal(url.origin, origin, 'No external requests');
  const request = {kind:'http', method:options.method || 'GET', target:url.pathname+url.search,
    headers:options.headers || {}, body:options.body == null ? null : String(options.body)};
  pending++;
  try {
    const raw = await rpc(request);
    assert.equal(raw.transport_error, undefined, raw.transport_error);
    const payload=(raw.headers.find(([k])=>k.toLowerCase()==='content-type')?.[1] || '')
      .includes('application/json') ? JSON.parse(raw.body) : null;
    requests.push({...request, status:raw.status, feedback:payload?.message || payload?.error});
    const response = new Response(raw.body, {status:raw.status, headers:raw.headers});
    Object.defineProperty(response, 'url', {value:url.href});
    if (options.redirect === 'error' && response.status >= 300 && response.status < 400)
      throw new Error('Unexpected redirect');
    return response;
  } finally { pending--; }
}
function mount(html, target) {
  if (dom) dom.window.close();
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => clientErrors.push(e.message));
  vc.on('error', (...args) => clientErrors.push(args.map(String).join(' ')));
  dom = new JSDOM(html, {url:new URL(target, origin).href, runScripts:'dangerously',
    virtualConsole:vc, beforeParse(window) { window.fetch = transport; }});
  for (const script of dom.window.document.scripts) {
    if (script.textContent.includes('form.js-inline-action'))
      scriptHashes.add(crypto.createHash('sha256').update(script.textContent).digest('hex'));
  }
  return dom.window.document;
}
async function navigate(target, options) {
  let response = await transport(target, options);
  for (let n=0; response.status >= 300 && response.status < 400; n++) {
    assert.ok(n < 8, 'Redirect loop');
    target = response.headers.get('location');
    response = await transport(target);
  }
  assert.equal(response.status, 200, await response.clone().text());
  return mount(await response.text(), target);
}
function document() { return dom.window.document; }
function form(action) {
  const found = [...document().querySelectorAll('form.js-inline-action')]
    .filter(f => f.querySelector('input[name="action"]')?.value === action);
  assert.equal(found.length, 1, 'One generated '+action+' form');
  return found[0];
}
function localLink(prefix, root = document()) {
  const link = [...root.querySelectorAll('a[href]')].find(a => a.getAttribute('href').startsWith(prefix));
  assert.ok(link, 'Generated link '+prefix); return link.getAttribute('href');
}
async function checkpoint(label) {
  const state = await rpc({kind:'state'});
  const observation = {label, state, text:document().body.textContent.replace(/\s+/g,' ').trim(),
    url:dom.window.location.href};
  observations.push(observation); return observation;
}
async function click(action, expected = 200) {
  const f = form(action), card = f.closest('[data-action-card]');
  const button = f.querySelector('button[type="submit"]');
  assert.ok(button && !button.disabled, action+' is usable');
  const start = requests.length;
  button.click(); // Native DOM click -> native submit -> shipped listener -> FormData -> fetch.
  const deadline = Date.now()+15000;
  while (pending || card.dataset.actionPending === 'true') {
    assert.ok(Date.now()<deadline, 'Action did not settle');
    await new Promise(r => setTimeout(r, 5));
  }
  const submitted = requests.slice(start).filter(r => r.method === 'POST');
  assert.equal(submitted.length, 1, 'Exactly one actual submitted request');
  assert.equal(submitted[0].status, expected);
  assert.equal(card.dataset.actionPending, 'false');
  assert.equal(document().querySelectorAll('.js-card-controls button:disabled').length, 0);
  const notices = [...document().querySelectorAll('.js-action-feedback, #action-feedback [role]')];
  assert.ok(notices.some(n => n.textContent.trim()), 'Visible response feedback');
  assert.ok(notices.some(n => n.getAttribute('role') === (expected===200 ? 'status':'alert')));
  assert.ok(submitted[0].feedback && notices.some(n => n.textContent === submitted[0].feedback),
    'Visible feedback must equal this real response, not a previous notice');
  return checkpoint(action+':'+expected);
}
async function login() {
  await navigate('/login?next=/find-matches');
  const f = document().querySelector('form[action="/auth/google/start"]');
  assert.ok(f, 'Generated login form');
  await navigate(f.getAttribute('action'), {method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new dom.window.URLSearchParams(new dom.window.FormData(f))});
  await navigate(localLink('/__fixture/google/complete'));
}
const B = '/job/opportunity-7002?variant=7006';
const A = '/job/opportunity-7002?variant=7003';
function itemRows(state) { return state.items; }
function posting(state, suffix) { return itemRows(state).filter(r => r.opportunity_url.endsWith('/Posting'+suffix)); }
function unchanged(before, after) { assert.deepEqual(after, before, 'Rejected action cannot mutate history'); }
async function main() {
  await login();
  await navigate(B);
  const initial = await checkpoint('initial');
  assert.ok(localLink('https://jobs.example.test/PostingB'));
  if (mode === 'original') {
    for (const action of ['save','applied']) {
      const result = await click(action,400);
      assert.match(result.text,/Malformed action request\./);
      unchanged(initial.state,result.state);
    }
  } else if (mode === 'return') {
    assert.match(initial.text,/Applied/); assert.match(initial.text,/Reminder set/);
    assert.equal(posting(initial.state,'B').at(-1).visibility,'visible');
    await navigate(localLink('/tracker'));
    await navigate(localLink('/tracker/item?'));
    assert.ok(localLink('https://jobs.example.test/PostingB'));
    await checkpoint('fresh-process-return');
  } else if (mode === 'foreign') {
    const direct = await click('applied');
    const mine = posting(direct.state,'B').find(r => r.profile_id !== posting(initial.state,'B')[0].profile_id);
    const other = posting(initial.state,'B')[0];
    assert.ok(mine && other && mine.pipeline_item_id !== other.pipeline_item_id);
    const f = form('remind_later'), field=f.querySelector('[name="pipeline_item_id"]');
    const ownValue=field.value;
    field.value=other.pipeline_item_id; // Deliberate negative only; positive submissions use generated fields.
    const rejected=await click('remind_later',403);
    unchanged(direct.state,rejected.state);
    field.value=ownValue;
    await click('remind_later');
  } else {
    assert.equal(mode,'journey');
    // Reject a malformed identity with the same shipped click/submit path, then recover.
    const f=form('save'), field=f.querySelector('[name="opportunity_key"]');
    const valid=field.value; field.value='invalid-exact-posting';
    const rejected=await click('save',403); unchanged(initial.state,rejected.state); field.value=valid;
    const saved=await click('save');
    assert.match(saved.text,/Saved/); assert.equal(posting(saved.state,'B')[0].workflow_status,'saved');
    const applied=await click('applied');
    assert.match(applied.text,/Applied/); assert.equal(posting(applied.state,'B')[0].workflow_status,'applied');
    const reminded=await click('remind_later');
    const reminder=posting(reminded.state,'B')[0].reminder_at; assert.ok(reminder);
    assert.match(reminded.text,/Reminder set/);
    const hidden=await click('not_interested');
    assert.equal(posting(hidden.state,'B')[0].visibility,'hidden');
    assert.equal(posting(hidden.state,'B')[0].workflow_status,'applied');
    assert.equal(posting(hidden.state,'B')[0].reminder_at,reminder);
    await navigate(localLink('/find-matches'));
    assert.equal(document().querySelector('#opportunity-7006'),null);
    assert.ok(document().querySelector('#opportunity-7003'));
    assert.equal(document().querySelectorAll('.match-card').length,10);
    await checkpoint('hidden-matches');
    await navigate(localLink('/tracker'));
    const hiddenLink=[...document().querySelectorAll('a[href]')].find(a =>
      new URL(a.href).pathname==='/tracker' && new URL(a.href).searchParams.get('view')==='hidden');
    assert.ok(hiddenLink,'Generated Hidden filter link');
    await navigate(hiddenLink.getAttribute('href'));
    await navigate(localLink('/tracker/item?'));
    assert.ok(localLink('https://jobs.example.test/PostingB'));
    const returned=await checkpoint('hidden-exact-return');
    assert.match(returned.text,/Applied/); assert.match(returned.text,/Reminder set/);
    const restored=await click('show_again');
    assert.equal(posting(restored.state,'B')[0].visibility,'visible');
    assert.equal(posting(restored.state,'B')[0].workflow_status,'applied');
    assert.equal(posting(restored.state,'B')[0].reminder_at,reminder);
    await navigate(localLink('/find-matches'));
    assert.equal(document().querySelectorAll('.match-card').length,10);
    await checkpoint('restored-competes');
    await navigate(A);
    assert.ok(form('save')); assert.ok(form('applied'));
    assert.equal(document().querySelector('.js-card-status')?.textContent.includes('Applied'),false);
    const sibling=await checkpoint('sibling-unchanged');
    assert.deepEqual(posting(sibling.state,'A'),posting(initial.state,'A'));
    await navigate(localLink('/tracker'));
    await navigate(localLink('/tracker/item?'));
    assert.ok(localLink('https://jobs.example.test/PostingB'));
    await checkpoint('later-return');
  }
  assert.deepEqual(clientErrors,[], 'No client execution errors');
  dom.window.close();
  process.stdout.write(JSON.stringify({kind:'result',mode,observations,requests,
    scriptHashes:[...scriptHashes],clientErrors})+'\n');
}
main().then(()=>process.exit(0)).catch(e=>{console.error(e.stack);process.exit(1);});
