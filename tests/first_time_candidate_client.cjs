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
  if (dom) dom.window.close();
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => clientErrors.push(e.message));
  vc.on('error', (...args) => clientErrors.push(args.map(String).join(' ')));
  dom = new JSDOM(html, {url:new URL(target, origin).href, runScripts:'dangerously',
    virtualConsole:vc, beforeParse(window) { window.fetch = transport; window.HTMLElement.prototype.scrollIntoView=function(){}; }});
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
async function saveDraft() {
  document().querySelector('#save-manual-draft').click();
  const deadline=Date.now()+15000;
  while(pending || !/Draft saved/.test(document().querySelector('#manual-draft-status').textContent)) {
    if(/alert/.test(document().querySelector('#manual-draft-status').getAttribute('role'))) throw new Error(document().querySelector('#manual-draft-status').textContent);
    assert.ok(Date.now()<deadline,'Draft save did not settle');await new Promise(r=>setTimeout(r,5));
  }
}
async function importJourney(fixture) {
  await navigate(localLink('/account/profile/intake'));
  if(mode==='import-resume')await submit('form.resume-primary-action');
  if(mode!=='import-resume') {
  const upload=document().querySelector('form[enctype="multipart/form-data"]');assert.ok(upload);
  // Populate the synthetic file input; everything else comes from the rendered form.
  const bytes=Buffer.from(fixture.document,'base64');
  const file=new dom.window.File([bytes], 'labelled-offline-fixture.docx', {type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'});
  Object.defineProperty(upload.querySelector('[name="resume"]'),'files',{value:[file]});
  let submission;
  upload.addEventListener('submit',e=>{
    if(e.defaultPrevented)return;e.preventDefault();
    const data=new FormData();
    for(const [key,value] of new dom.window.FormData(upload)) {
      if(typeof value==='string')data.append(key,value);
      else if(key==='resume')data.append(key,new Blob([bytes],{type:file.type}),file.name);
    }
    submission=(async()=>{const req=new Request(origin+upload.getAttribute('action'),{method:'POST',body:data});
      await navigate(upload.getAttribute('action'),{method:'POST',headers:{'Content-Type':req.headers.get('content-type')},
        body:Buffer.from(await req.arrayBuffer()).toString('base64'),bodyEncoding:'base64'});})();
  },{once:true});
  upload.querySelector('button[type="submit"]').click();assert.ok(submission);await submission;
  }
  const draft=await checkpoint('labelled-offline-extraction-review');assert.equal(draft.state.profiles.length,0);
  assert.equal(document().querySelector('[data-display-name]').value,mode==='import-resume'?'Reviewed Synthetic Candidate':'Synthetic Candidate');
  const name=document().querySelector('[data-display-name]');assert.ok(name);name.value='Reviewed Synthetic Candidate';name.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
  const next=[...document().querySelectorAll('[data-confirm-profile-basics], [data-confirm-background]')];
  assert.ok(next.length,'Review step controls');
  for(const button of next){button.click();await settle();}
  if(mode==='import-draft') {await checkpoint('interrupted-import');return;}
  const f=document().querySelector('#profile-review-form')||document().querySelector('form.intake-review-form');assert.ok(f);
  // The real script owns step confirmation/autosave tokens; final native submit
  // uses the resulting rendered form exactly as a browser would.
  await submit('#'+f.id);
  const confirmation=requests.filter(r=>r.method==='POST' && r.target.startsWith('/account/profile/intake/review') && r.status===303).at(-1);
  assert.ok(confirmation,'Native import confirmation completed');
  const replay=await transport(confirmation.target,{method:'POST',headers:confirmation.headers,body:confirmation.body});
  assert.equal(replay.status,303,'Repeated import confirmation returns the existing profile');
  const saved=await checkpoint('import-confirmed');
  assert.equal(saved.state.profiles.length,1);
  assert.equal(saved.state.attempts.filter(a=>a.state==='succeeded').length,1);
  await navigate('/account/profile');assert.match(document().body.textContent,/Reviewed Synthetic Candidate/);
}
async function settle(){const deadline=Date.now()+15000;do{await new Promise(r=>setTimeout(r,30));assert.ok(Date.now()<deadline);}while(pending);}
async function main() {
  const fixture=await rpc({kind:'fixture'});
  await navigate('/login?next=/account/profile');
  set('invitation',fixture.invitation);
  await submit('form[action="/auth/google/start"]');
  await navigate(localLink('/__fixture/google/complete'));
  const first=await checkpoint('account-entry');
  if(mode==='candidate-return') {
    assert.equal(first.state.profiles.length,1);assert.equal(first.state.users.length,1);
    assert.match(document().body.textContent,/Salvador/);await navigate('/tracker');await navigate(localLink('/tracker/item?'));
    assert.match(document().body.textContent,/Applied/);assert.match(document().body.textContent,/Reminder set/);
    await checkpoint('completed-profile-return');
    process.stdout.write(JSON.stringify({kind:'result',observations,requests,scriptHashes:[...scriptHashes]})+'\n');
    dom.window.close();return;
  }
  assert.match(first.text,/Create your profile/);
  if(!['import','import-draft'].includes(mode))assert.match(first.text,/Document import is currently unavailable/);
  assert.equal(first.state.profiles.length,0);
  assert.equal(first.state.users.length,1);
  if(mode.startsWith('import')) {
    await importJourney(fixture);
    process.stdout.write(JSON.stringify({kind:'result',observations,requests,scriptHashes:[...scriptHashes]})+'\n');
    dom.window.close();return;
  }
  const unavailable=await transport(localLink('/account/profile/intake'));
  assert.equal(unavailable.status,503);
  const unavailableHTML=await unavailable.text();assert.match(unavailableHTML,/Document import unavailable/);
  assert.ok(!/type=['"]file['"]/.test(unavailableHTML),'Disabled import cannot invite an unusable upload');
  await navigate(localLink('/find-matches'));
  if(mode==='resume') {
    assert.equal(document().querySelector('[name="city"]').value,'Recife');
  } else {
    set('input_text',mode==='empty'?'I live in Canada.':fixture.background);
    await saveDraft();
    await navigate('/find-matches');
    assert.ok(document().querySelector('[name="input_text"]').value);
    await submit('#find-matches-form');
    assert.ok(document().querySelector('#profile-review-form'));
    // Edit the description and refresh the actual edit URL after autosave.
    if(mode==='manual') {
      await navigate(localLink('/find-matches?'));
      const updated=fixture.background+' I live in Recife.';
      set('input_text',updated);await saveDraft();
      await navigate(dom.window.location.pathname+dom.window.location.search);
      assert.equal(document().querySelector('[name="input_text"]').value,updated);
      await submit('#find-matches-form');
    }
    assert.equal(document().querySelector('[name="credentials_confirmed"]').checked,false);
    if(mode==='empty') {
      for(const name of ['skills','professional_domains','degrees','education_fields','licenses','certifications','target_opportunity_types'])
        assert.equal(document().querySelector('[name="'+name+'"]').value,'','Missing '+name+' must remain unknown');
    }
    assert.equal(document().querySelector('#item-experience-dialog'),null,'Correction-only item experience is not offered during creation');
    assert.equal(document().querySelector('[data-add-education]'),null,'Creation retains independent education fields');
    if(mode==='manual') {
      document().querySelector('[data-add-employment]').click();
      const entry=[...document().querySelectorAll('[data-employment-value]')].at(-1);
      entry.value='Built synthetic tools, Example Co, 2022–2024';entry.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
      await saveDraft();await navigate(dom.window.location.pathname+dom.window.location.search);
      const saved=[...document().querySelectorAll('[data-employment-entry]')].find(e=>e.querySelector('textarea').value.includes('Example Co'));
      assert.ok(saved);saved.querySelector('[data-remove-employment]').click();
      await new Promise(r=>setTimeout(r,800));await settle();
      assert.match(document().querySelector('#manual-draft-status').textContent,/Draft saved/);
      await navigate(dom.window.location.pathname+dom.window.location.search);
      assert.ok(![...document().querySelectorAll('[data-employment-value]')].some(e=>e.value.includes('Example Co')),'Removal survives autosave and refresh');
    }
    const stale=new dom.window.URLSearchParams(new dom.window.FormData(document().querySelector('#profile-review-form')));
    set('city','Recife');
    if(mode!=='empty')set('language_1','Portuguese');
    await saveDraft();
    stale.set('city','Stale City');stale.set('manual_action','save');stale.delete('credentials_confirmed');
    const rejected=await transport('/find-matches',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body:stale.toString()});
    assert.equal(rejected.status,409,'An old rendered form cannot replace newer saved input');
    stale.delete('manual_action');stale.set('credentials_confirmed','1');
    const nativeStale=await transport('/find-matches',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:stale.toString()});
    assert.equal(nativeStale.status,409);
    const currentURL=dom.window.location.pathname+dom.window.location.search;
    mount(await nativeStale.text(),'/find-matches');
    assert.ok(localLink('/find-matches'),'Native stale response has a resume route');
    await navigate(currentURL);
    assert.equal(document().querySelector('[name="city"]').value,'Recife','Refresh the actual review URL restores saved corrections');
    if(mode==='manual') {
      set('country','This is not a country');document().querySelector('[name="credentials_confirmed"]').checked=true;
      await submit('#profile-review-form',400);
      assert.equal(document().querySelector('[name="country"]').value,'This is not a country');
      assert.match(document().querySelector('[role="alert"]').textContent,/country name/);
      set('country','Brazil');await saveDraft();
    }
    if(mode==='draft') {
      const saved=await checkpoint('unconfirmed-review');
      assert.equal(saved.state.profiles.length,0);
      process.stdout.write(JSON.stringify({kind:'result',observations,requests,scriptHashes:[...scriptHashes]})+'\n');
      dom.window.close();return;
    }
  }
  const before=await checkpoint('before-confirmation');
  assert.equal(before.state.profiles.length,0);
  document().querySelector('[name="credentials_confirmed"]').checked=true;
  await submit('#profile-review-form');
  assert.match(document().body.textContent,/Your reviewed profile is ready/);
  assert.equal((await rpc({kind:'state'})).profiles.length,0);
  if(mode==='confirmation-draft') {
    await checkpoint('interrupted-before-create');
    process.stdout.write(JSON.stringify({kind:'result',observations,requests,scriptHashes:[...scriptHashes]})+'\n');
    dom.window.close();return;
  }
  const creationForm=document().querySelector('form[action="/account/profile"]');
  const repeatedCreation=new dom.window.URLSearchParams(new dom.window.FormData(creationForm)).toString();
  await submit('form[action="/account/profile"]');
  const repeated=await transport('/account/profile',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:repeatedCreation});
  assert.equal(repeated.status,303,'Lost response retry returns the same created profile');
  const confirmed=await checkpoint('confirmed-matches');
  assert.equal(confirmed.state.profiles.length,1);
  assert.equal(confirmed.state.revisions.length,1);
  if(mode==='empty') {
    assert.equal(document().querySelectorAll('.match-card').length,0);
  } else {
    await navigate(localLink('/job/'));
    const saved=await click('save');
    assert.equal(saved.state.items.length,1);
    await navigate(localLink('/tracker'));
    await navigate(localLink('/tracker/item?'));
    assert.match(document().body.textContent,/Saved/);
    await click('applied');await click('remind_later');
    const history=await rpc({kind:'state'});
    await navigate('/account/profile');
    assert.match(document().body.textContent,/Recife/);
    await navigate(localLink('/account/profile?correction=start'));
    await submit('form');
    await navigate([...document().querySelectorAll('a')].find(a=>a.textContent==='Edit profile').getAttribute('href'));
    set('city','Salvador');document().querySelector('[name="credentials_confirmed"]').checked=true;
    await submit('#profile-review-form');
    document().querySelector('[name="confirmed"]').checked=true;
    await submit('form');
    assert.match(document().body.textContent,/Salvador/);
    const corrected=await rpc({kind:'state'});
    assert.equal(corrected.revisions.length,2);
    assert.deepEqual(corrected.items,history.items);assert.deepEqual(corrected.transitions,history.transitions);
    await navigate('/tracker');await navigate(localLink('/tracker/item?'));
    assert.match(document().body.textContent,/Applied/);assert.match(document().body.textContent,/Reminder set/);
  }
  const finish=await checkpoint('complete');
  assert.equal(finish.state.profiles.length,1);
  process.stdout.write(JSON.stringify({kind:'result',observations,requests,scriptHashes:[...scriptHashes]})+'\n');
  dom.window.close();
}
main().catch(e=>{process.stderr.write(e.stack+'\n');if(dom)dom.window.close();process.exitCode=1;}).finally(()=>process.stdin.destroy());
