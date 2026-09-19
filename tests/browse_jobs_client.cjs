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
  await navigate('/login?next=/jobs');
  const f = document().querySelector('form[action="/auth/google/start"]');
  assert.ok(f, 'Generated login form');
  await navigate(f.getAttribute('action'), {method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new dom.window.URLSearchParams(new dom.window.FormData(f))});
  await navigate(localLink('/__fixture/google/complete'));
}

async function search(values) {
  const f=document().querySelector('form.catalog-filters');assert.ok(f);
  for(const [key,value] of Object.entries(values))f.elements.namedItem(key).value=value;
  let navigation;
  f.addEventListener('submit', e=>{
    e.preventDefault();
    const query=new dom.window.URLSearchParams(new dom.window.FormData(f));
    assert.equal(query.has('page'),false,'Changing filters resets the page');
    navigation=navigate(f.getAttribute('action')+'?'+query);
  },{once:true});
  f.querySelector('button[type=submit]').click();
  assert.ok(navigation,'Native form submit');await navigation;
}
const ids=()=>[...document().querySelectorAll('.job-card h2 a')].map(a=>a.pathname);
const linkText=(text)=>[...document().querySelectorAll('a')].find(a=>a.textContent.trim()===text)?.getAttribute('href');
async function main(){
  await login();
  assert.match(document().querySelector('h1').textContent,/Browse jobs/);
  assert.equal(document().querySelector('nav a[aria-current=page]').textContent,'Browse jobs');
  const original=await rpc({kind:'state'});
  await search({q:'PaginationFixture',location:'Brazil'});
  assert.equal(ids().length,30); const first=ids();
  assert.match(document().querySelector('.catalog-summary').textContent,/65 current opportunities/);
  assert.match(document().querySelector('.active-filters').textContent,/Brazil/);
  await navigate(linkText('Next')); const second=ids();
  assert.equal(second.length,30);assert.equal(second.some(x=>first.includes(x)),false);
  const context=dom.window.location.pathname+dom.window.location.search;
  assert.match(context,/page=2/);
  const detail=document().querySelector('.job-card .view-job').getAttribute('href');
  assert.equal(new URL(detail,origin).searchParams.get('return_to'),context);
  assert.ok(new URL(detail,origin).searchParams.get('variant'));
  await navigate(detail);
  const back=document().querySelector('.back-to-jobs a').getAttribute('href');assert.equal(back,context);
  assert.equal(document().querySelector('#recommendation-personalization'),null);
  const external=document().querySelector('.hero-actions a');assert.equal(external.target,'_blank');
  assert.deepEqual((await rpc({kind:'state'})).items,original.items,'Reading/app link is not Applied');
  await navigate(back); assert.deepEqual(ids(),second,'Return restores order and page');
  await navigate(context);assert.deepEqual(ids(),second,'Reload preserves the URL state');
  await navigate(detail);await click('save');
  const saved=await rpc({kind:'state'});assert.equal(saved.items.length,original.items.length+1);
  await navigate(context);assert.match(document().querySelector('.catalog-saved').textContent,/Saved/);
  await navigate(detail);assert.match(document().querySelector('.js-card-status').textContent,/Saved/);
  await click('applied');
  await navigate(context);assert.match(document().querySelector('.catalog-saved').textContent,/Applied/);
  await navigate('/tracker');assert.match(document().body.textContent,/PaginationFixture/);
  const final=await rpc({kind:'state'});assert.equal(final.items.length,saved.items.length);
  assert.deepEqual(final.revisions,original.revisions,'Browse never edits a profile');
  await navigate(context);await search({q:'NothingMatchesFixture'});
  assert.match(document().body.textContent,/No jobs match these filters/);
  assert.equal(new URL(dom.window.location.href).searchParams.has('page'),false);
  assert.equal(new URL(dom.window.location.href).searchParams.get('location'),'Brazil');
  const remove=document().querySelector('a[aria-label="Remove Keyword filter: NothingMatchesFixture"]');
  await navigate(remove.getAttribute('href'));assert.equal(document().querySelector('[name=q]').value,'');
  assert.equal(document().querySelector('[name=location]').value,'Brazil');
  await navigate(linkText('Clear filters'));assert.equal(dom.window.location.pathname+dom.window.location.search,'/jobs');
  await checkpoint('browse-complete');
  assert.deepEqual(clientErrors,[]);
  assert.ok(scriptHashes.size>0,'Actual shipped workflow JavaScript executed');
  process.stdout.write(JSON.stringify({kind:'result',observations,requests,scriptHashes:[...scriptHashes]})+'\n');
  dom.window.close();
}
main().catch(e=>{process.stderr.write(e.stack+'\n');if(dom)dom.window.close();process.exitCode=1;}).finally(()=>process.stdin.destroy());
