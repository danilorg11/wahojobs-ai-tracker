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
    bodyEncoding:options.bodyEncoding, headers:options.headers || {}, body:options.body == null ? null : String(options.body)};
  pending++;
  try {
    const raw = await rpc(request);
    assert.equal(raw.transport_error, undefined, raw.transport_error);
    const payload=(raw.headers.find(([k])=>k.toLowerCase()==='content-type')?.[1] || '')
      .includes('application/json') ? JSON.parse(raw.body) : null;
    requests.push({...request, status:raw.status, feedback:payload?.message || payload?.error});
    const response = new Response([204,205,304].includes(raw.status)?null:raw.body, {status:raw.status, headers:raw.headers});
    Object.defineProperty(response, 'url', {value:url.href});
    if (options.redirect === 'error' && response.status >= 300 && response.status < 400)
      throw new Error('Unexpected redirect');
    return response;
  } finally { pending--; }
}
function mount(html, target, policy) {
  assert.ok(policy, 'Every served page includes its CSP policy');
  if (dom) dom.window.close();
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => clientErrors.push(e.message));
  vc.on('error', (...args) => clientErrors.push(args.map(String).join(' ')));
  dom = new JSDOM(html, {url:new URL(target, origin).href, runScripts:'dangerously',
    virtualConsole:vc, beforeParse(window) { window.fetch = transport; window.HTMLElement.prototype.scrollIntoView=function(){}; window.HTMLDialogElement.prototype.showModal=function(){this.open=true;}; window.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new window.Event("close"));}; }});
  for (const script of dom.window.document.scripts) {
    if (script.textContent) {
      scriptHashes.add(crypto.createHash('sha256').update(script.textContent).digest('hex'));
      if(policy)assert.ok(policy.includes("'sha256-"+crypto.createHash('sha256').update(script.textContent).digest('base64')+"'"),
        'The served CSP authorizes this exact shipped script');
    }
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
  assert.equal(response.status, 200, (await response.clone().text()).slice(0,800));
  return mount(await response.text(), target, response.headers.get('content-security-policy'));
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
  const observation = {label, state, text:[...document().body.querySelectorAll('h1,h2,h3,p,button,a')].map(e=>e.textContent).join(' ').replace(/\s+/g,' ').trim(),
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
async function submit(selector, expected=200) {
  const f = document().querySelector(selector);
  assert.ok(f, 'Rendered form '+selector);
  let navigation;
  f.addEventListener('submit', e => {
    if (e.defaultPrevented) return;
    e.preventDefault();
    const data = new dom.window.URLSearchParams(new dom.window.FormData(f,e.submitter));
    const options={method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:data.toString()};
    navigation = expected===200 ? navigate(f.getAttribute('action'), options) :
      transport(f.getAttribute('action'), options).then(async response=>{
        assert.equal(response.status,expected);mount(await response.text(),f.getAttribute('action'),response.headers.get('content-security-policy'));
      });
  });
  f.querySelector('button[type="submit"]').click();
  const deadline=Date.now()+15000;
  while(!navigation && Date.now()<deadline)await new Promise(r=>setTimeout(r,20));
  assert.ok(navigation, 'Native click passed form validation and reached browser submission');
  await navigation;
}
function set(name,value) { const e=document().querySelector('[name="'+name+'"]');assert.ok(e,name); e.value=value; }
function textLink(label) {
  const link=[...document().querySelectorAll('a[href]')].find(a=>a.textContent.trim()===label);
  assert.ok(link,'Generated link '+label);return link.getAttribute('href');
}
function checked(selector,value=true) {
  const input=document().querySelector(selector);assert.ok(input,selector);input.checked=value;
  input.dispatchEvent(new dom.window.Event('change',{bubbles:true}));
}
async function main() {
  await login();
  const initial=await checkpoint('matches');
  if(mode==='decision-preferences') {
    await navigate(textLink('My profile'));
    assert.match(document().body.textContent,/Preferred minimum: USD 15\/hour/);
    await navigate(textLink('Update profile'));await submit('form');await navigate(textLink('Edit profile'));
    const before=await rpc({kind:'state'});
    set('beta_pay_0_amount','invalid');set('city','Example preference city');
    checked('[name=credentials_confirmed]');await submit('#profile-review-form',400);
    assert.match(document().body.textContent,/Review your work preferences/);
    assert.equal(document().querySelector('[name=beta_pay_0_amount]').value,'invalid');
    assert.equal(document().querySelector('[name=city]').value,'Example preference city');
    assert.deepEqual((await rpc({kind:'state'})).revisions,before.revisions);
    set('beta_pay_0_amount','18');checked('[name=credentials_confirmed]');
    checked('[name=beta_preference_workloads][value=part_time]',false);
    checked('[name=beta_preference_workloads][value=full_time]');
    await submit('#profile-review-form');
    assert.match(document().body.textContent,/Preferred minimum: USD 18\/hour/);
    assert.deepEqual((await rpc({kind:'state'})).revisions,before.revisions,'Review remains a draft');
    checked('[name=confirmed]');await submit('form');
    assert.match(document().body.textContent,/Profile changes saved/);
    const after=await rpc({kind:'state'});
    assert.equal(after.revisions.length,before.revisions.length+1);
    const original=JSON.parse(before.revisions[0].structured_profile_json),current=JSON.parse(after.revisions.at(-1).structured_profile_json);
    assert.deepEqual(current.preferences.preference_model.workloads,['full_time']);
    assert.equal(current.preferences.preference_model.compensation_expectations[0].amount,'18');
    assert.deepEqual(current.preferences.preference_model.accepted_phone_voice_modes,['non_phone']);
    assert.deepEqual(current.identity,original.identity);
    assert.deepEqual(after.items,before.items);assert.deepEqual(after.transitions,before.transitions);
    await navigate(textLink('My profile'));
    assert.match(document().body.textContent,/Preferred minimum: USD 18\/hour/);
    await navigate(textLink('Matches'));await checkpoint('typed-preferences-confirmed');
  } else if(mode==='decision-preferences-return') {
    await navigate(textLink('My profile'));
    assert.match(document().body.textContent,/Preferred minimum: USD 18\/hour/);
    assert.match(document().body.textContent,/Example preference city/);
    await checkpoint('typed-preferences-fresh-return');
  } else if(mode==='decision-return') {
    const item=initial.state.items[0];assert.ok(item);
    await navigate('/tracker');await navigate(localLink('/tracker/item?'));
    assert.match(document().body.textContent,/Applied/);assert.match(document().body.textContent,/Reminder set/);
    assert.match(document().body.textContent,/Before you apply/);
    assert.equal(document().querySelector('.candidate-profile-update a[href*="focus=skills"]'),null);
    assert.ok(!document().querySelector('.decision-warning')?.textContent.includes('Experience with Python'),'Confirmed tool context is no longer a missing-condition warning');
    await checkpoint('fresh-process-return');
  } else {
    assert.equal(mode,'decision');
    assert.ok(document().querySelectorAll('article[data-action-card]').length>=2,'Several normally selected opportunities');
    assert.ok(document().body.textContent.includes('Alignerr'));
    assert.ok(document().querySelector('.match-card#opportunity-9403'),'Grounded recommendation remains delivered');
    assert.ok(document().querySelector('.match-card#opportunity-9400'),'Preserved Alignerr posting remains in the unified list');
    const card=document().querySelector('#opportunity-7003,#opportunity-7006');assert.ok(card,'Normally selected Python opportunity');
    const target=localLink('/job/',card), oldRun=new URL(target,origin).searchParams.get('run');
    assert.ok(oldRun);const oldMatches='/find-matches?run='+oldRun;
    await navigate(target);
    assert.ok(document().querySelector('.candidate-profile-update'),'Exact detail offers an optional supported correction');
    assert.match(document().body.textContent,/Experience with Python/);
    await click('save');await click('remind_later');
    const saved=await rpc({kind:'state'});
    const staleFields=form('applied').outerHTML;
    await navigate(localLink('/account/profile?correction=start'));
    await submit('form');await navigate(textLink('Edit profile'));
    assert.equal(document().querySelector('#profile-review-form').dataset.focus,'skills');
    const row=[...document().querySelectorAll('#skills [data-collection-item]')].find(r=>r.querySelector('input:not([type=checkbox])').value==='Python');
    assert.ok(row,'Existing canonical Python is reused');
    row.querySelector('[data-experience-open]').click();
    let dialog=document().querySelector('#item-experience-dialog');assert.ok(dialog.open);
    assert.equal(dialog.querySelector('[data-item-autonomy]').value,'unknown');
    dialog.querySelector('[data-item-autonomy]').value='independent';
    dialog.querySelector('[data-item-cancel]').click();
    assert.equal(document().querySelector('[name=item_experience]').value,'[]','Cancel retains uncertainty');
    row.querySelector('[data-experience-open]').click();
    checked('[data-item-context][value=professional]');
    dialog.querySelector('[data-item-autonomy]').value='independent';
    dialog.querySelector('[data-item-autonomy]').dispatchEvent(new dom.window.Event('change',{bubbles:true}));
    dialog.querySelector('[data-item-save]').click();
    assert.equal(JSON.parse(document().querySelector('[name=item_experience]').value)[0].months,null);
    checked('[name=credentials_confirmed]');set('country','Invalid fixture country');
    await submit('#profile-review-form',400);
    assert.match(document().body.textContent,/Enter a country name/);
    assert.equal(document().querySelector('button[type=submit]').disabled,false);
    assert.deepEqual((await rpc({kind:'state'})).revisions,saved.revisions,'Invalid draft cannot change authority');
    set('country','Brazil');checked('[name=credentials_confirmed]');
    await submit('#profile-review-form');
    assert.match(document().body.textContent,/Confirm this correction/);
    const proposal=await checkpoint('review-before-confirmation');
    assert.deepEqual(proposal.state.revisions,saved.revisions);
    assert.match(document().body.textContent,/self-reported/);
    checked('[name=confirmed]');await submit('form');
    assert.match(document().body.textContent,/Profile changes saved/);
    assert.ok(textLink('Return to opportunity'));
    const appliedProfile=await rpc({kind:'state'});
    assert.equal(appliedProfile.revisions.length,saved.revisions.length+1);
    assert.deepEqual(appliedProfile.items,saved.items);assert.deepEqual(appliedProfile.transitions,saved.transitions);
    assert.deepEqual(appliedProfile.revisions.slice(0,2),saved.revisions,'Existing owner revisions stay immutable');
    const previous=JSON.parse(saved.revisions[0].structured_profile_json),current=JSON.parse(appliedProfile.revisions.at(-1).structured_profile_json);
    assert.deepEqual(current.education,previous.education);assert.deepEqual(current.languages,previous.languages);
    await navigate(textLink('Return to opportunity'));
    assert.match(document().body.textContent,/Before you apply/);
    assert.equal(document().querySelector('.candidate-profile-update a[href*="focus=skills"]'),null,'No repeated request for confirmed tool evidence');
    assert.ok(!document().querySelector('.decision-warning')?.textContent.includes('Experience with Python'),'Actual corrected comparison no longer requests the confirmed tool evidence');
    await checkpoint('corrected-exact-assessment');
    // A profile correction does not revoke a still-current workflow decision.
    const staleReminder=form('remind_later').outerHTML;
    const freshForm=form('applied');freshForm.outerHTML=staleFields;
    await click('applied');
    const beforeStale=await rpc({kind:'state'});
    form('remind_later').outerHTML=staleReminder;
    await click('remind_later',409);
    assert.deepEqual((await rpc({kind:'state'})).transitions,beforeStale.transitions);
    await navigate(target);
    await click('not_interested');
    await navigate('/tracker?view=hidden');await navigate(localLink('/tracker/item?'));
    assert.match(document().body.textContent,/Applied/);assert.match(document().body.textContent,/Reminder set/);
    await click('show_again');
    await navigate(oldMatches);await checkpoint('old-results-recomputed');
    await navigate('/tracker');await navigate(localLink('/tracker/item?'));
    assert.match(document().body.textContent,/Before you apply/);
    assert.match(document().body.textContent,/Applied/);
    await checkpoint('workflow-preserved');
  }
  assert.deepEqual(clientErrors,[]);
  process.stdout.write(JSON.stringify({kind:'result',observations,requests,scriptHashes:[...scriptHashes]})+'\n');
  dom.window.close();
}
main().catch(e=>{process.stderr.write(e.stack+'\n');if(dom)dom.window.close();process.exitCode=1;}).finally(()=>process.stdin.destroy());
