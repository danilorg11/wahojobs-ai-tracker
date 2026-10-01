const assert = require('node:assert/strict');
const vm = require('node:vm');
const test = require('node:test');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const source = input.bootstrap;
const id = 'G-QFMW1WX907';
function fixture(path = '/jobs', origin = 'https://www.wahojobs.com') {
  const scripts = [];
  const window = {location: {origin, pathname: path,
    href: origin + path + '?email=private%40example.test&token=secret#otp=123456'}};
  const document = {referrer: origin + '/candidate/auth/callback?code=private',
    head: {appendChild: tag => scripts.push(tag)},
    createElement: () => ({setAttribute() {},contentWindow:{},remove(){this.removed=true;}})};
  const context = vm.createContext({window, document, Date});
  vm.runInContext(source, context);
  const commands = () => (window.dataLayer || []).map(a => Array.from(a));
  const events = () => commands().filter(c => c[0] === 'event');
  const resolve = data => {
    window.ezCMPQueue = {gotResponse: true, push: callback => callback()};
    window.ezTcfConsent = {loaded: true, store_info: true};
    if (data) window.__tcfapi = (command, version, callback) => {
      assert.equal(command, 'addEventListener'); assert.equal(version, 2);
      window.notify = callback; callback(data, true);
    };
    scripts[0].onload();
  };
  return {window, scripts, context, commands, events, resolve};
}
function tcf(overrides = {}) {
  return {cmpStatus: 'loaded', gdprApplies: true, eventStatus: 'useractioncomplete',
    purpose: {consents: {1: true}}, vendor: {consents: {755: true}}, ...overrides};
}
test('unknown / failed CMP never loads Google or queues a pageview', () => {
  const f = fixture(); assert.equal(f.scripts.length, 1);
  assert.equal(f.window['ga-disable-' + id], true); assert.equal(f.events().length, 0);
  f.scripts[0].onload(); assert.equal(f.scripts.length, 1);
});
test('CMP region must finish before the no-TCF decision is usable', () => {
  const f = fixture(); let resolve;
  f.window.ezCMPQueue = {gotResponse: false, push: fn => { resolve = fn; }};
  f.window.ezTcfConsent = {loaded: true, store_info: true};
  f.scripts[0].onload(); assert.equal(f.events().length, 0);
  resolve(); assert.equal(f.events().length, 0);
  f.window.ezCMPQueue.gotResponse = true; resolve(); assert.equal(f.events().length, 1);
});
test('existing regional no-TCF policy enables one sanitized view', () => {
  const f = fixture('/jobs/opportunity-2250'); f.resolve();
  assert.equal(f.scripts.length, 2); assert.equal(f.events().length, 1);
  assert.equal(f.scripts[1].src, '/jobs/_analytics');
  assert.equal(f.scripts[1].hidden, true);
  const config = f.commands().find(c => c[0] === 'config')[2];
  assert.equal(config.send_page_view, false); assert.equal(config.allow_google_signals, false);
  assert.equal(config.page_location, 'https://www.wahojobs.com/jobs/opportunity-2250');
  assert.equal(config.page_referrer, '');
  assert(!JSON.stringify(f.commands()).match(/email=|token=|otp=|private|callback|variant=|return_to=/));
});
for (const [name, data] of Object.entries({
  deniedPurpose: tcf({purpose: {consents: {1: false}}}),
  deniedVendor: tcf({vendor: {consents: {755: false}}}),
  missingVendor: tcf({vendor: undefined}),
  missingPurpose: tcf({purpose: undefined}),
  loading: tcf({cmpStatus: 'loading'}),
  unresolvedRegion: tcf({gdprApplies: undefined}),
  pendingUI: tcf({eventStatus: 'cmpuishown'})
})) test(name + ' does not load Google', () => {
  const f = fixture(); f.resolve(data); assert.equal(f.events().length, 0);
  assert.equal(f.scripts.length, 1); assert.equal(f.window['ga-disable-' + id], true);
});
test('stored TCF grant, repeated callbacks, revocation and regrant never duplicate', () => {
  const f = fixture(); f.resolve(tcf({eventStatus: 'tcloaded'}));
  f.window.notify(tcf(), true); assert.equal(f.events().length, 1);
  f.window.notify(tcf({purpose: {consents: {1: false}}}), true);
  assert.equal(f.window['ga-disable-' + id], true);
  assert.equal(f.scripts[1].contentWindow['ga-disable-' + id], true);
  assert.equal(f.scripts[1].removed, true);
  f.window.notify(tcf(), true); assert.equal(f.window['ga-disable-' + id], false);
  assert.equal(f.events().length, 1); assert.equal(f.scripts.length, 2);
});
test('withdrawal survives an unavailable frame and still removes it',()=>{
  const f=fixture();f.resolve(tcf());
  Object.defineProperty(f.scripts[1],'contentWindow',{get(){throw Error('blocked frame');}});
  assert.doesNotThrow(()=>f.window.notify(tcf({purpose:{consents:{1:false}}}),true));
  assert.equal(f.window['ga-disable-'+id],true);assert.equal(f.scripts[1].removed,true);
});
test('CMP explicit non-GDPR decision grants without inventing purpose consents', () => {
  const f = fixture(); f.resolve(tcf({gdprApplies: false, purpose: undefined}));
  assert.equal(f.events().length, 1);
});
test('CMP does not enable ads, URL passthrough or arbitrary events', () => {
  const f = fixture(); f.resolve(tcf());
  f.window.gtag('consent', 'update', {ad_storage: 'granted', analytics_storage: 'granted'});
  f.window.gtag('set', 'url_passthrough', true);
  f.window.gtag('event', 'login', {email: 'private@example.test'});
  assert.equal(f.events().length, 1);
  const last = f.commands().at(-1)[2]; assert.equal(last.ad_storage, 'denied');
  assert.equal(last.ad_user_data, 'denied'); assert.equal(last.ad_personalization, 'denied');
  assert(!f.commands().some(c => c[1] === 'url_passthrough' && c[2] === true));
});
test('duplicate bootstrap cannot add another CMP or pageview', () => {
  const f = fixture(); f.resolve(tcf()); vm.runInContext(source, f.context);
  assert.equal(f.scripts.length, 2); assert.equal(f.events().length, 1);
});
for (const path of ['/login', '/candidate/auth/callback', '/account/profile', '/my-jobs',
  '/jobs/sitemap.xml', '/jobs/opportunity-0', '/jobs/anything@example.test'])
  test('excluded route ' + path, () => {
    const f = fixture(path); assert.equal(f.scripts.length, 0); assert.equal(f.events().length, 0);
  });
test('preview and beta origins never send to the production property', () => {
  const f = fixture('/jobs', 'https://beta.wahojobs.com'); assert.equal(f.scripts.length, 0);
});
test('nested catalog documents cannot recursively initialize', () => {
  const f = fixture('/jobs', 'https://beta.wahojobs.com');
  f.window.location.origin='https://www.wahojobs.com'; f.window.parent={};
  vm.runInContext(source,f.context); assert.equal(f.scripts.length,0);
});
function collector() {
  const sends=[], tags=[];
  const parent = {['ga-disable-'+id]:false,location:{pathname:'/jobs'}};
  const window = {parent,location:{origin:'https://www.wahojobs.com'},
    addEventListener:(type,fn)=>{window.notify=fn;},removeEventListener(){},
    fetch:(url,options)=>{sends.push(['fetch',url]);return Promise.resolve();}};
  const navigator={sendBeacon:(url,body)=>{sends.push(['beacon',url]);return true;}};
  function XMLHttpRequest() {}
  XMLHttpRequest.prototype.open=function(){};
  XMLHttpRequest.prototype.send=function(){sends.push(['xhr']);};
  XMLHttpRequest.prototype.abort=function(){this.aborted=true;};
  const document={head:{appendChild:tag=>tags.push(tag)},createElement:()=>({})};
  const context=vm.createContext({window,navigator,document,XMLHttpRequest,Date,URL,URLSearchParams,Promise,Response});
  vm.runInContext(input.frame.match(/<script>([\s\S]*?)<\/script>/)[1],context);
  window.notify({source:parent,origin:'https://www.wahojobs.com',
    data:{type:'wahojobs-public-measurement',commands:'[]'}});
  return {window,parent,navigator,sends,tags,XMLHttpRequest};
}
function request(overrides={}) {
  return 'https://www.google-analytics.com/g/collect?'+new URLSearchParams({
    tid:id,en:'page_view',dl:'https://www.wahojobs.com/jobs',dr:'',
    dt:'AI Training Jobs | Wahojobs',...overrides});
}
test('empty collector permits one clean view and blocks duplicates',()=>{
  const f=collector();assert.equal(f.tags.length,1);
  f.navigator.sendBeacon(request());f.navigator.sendBeacon(request());
  assert.equal(f.sends.length,1);
});
for(const [name,overrides] of Object.entries({scroll:{en:'scroll'},
  auth:{dl:'https://www.wahojobs.com/candidate/auth/callback'},
  query:{dl:'https://www.wahojobs.com/jobs?q=private@example.test'},
  referrer:{dr:'https://www.wahojobs.com/login?otp=123456'},
  userID:{uid:'private'},custom:{'ep.search_term':'private'},
  userProperty:{'up.email':'private'},wrongProperty:{tid:'G-OTHER'},
  personalTitle:{dt:'private@example.test'}}))test('transport rejects '+name,()=>{
    const f=collector();f.navigator.sendBeacon(request(overrides));assert.equal(f.sends.length,0);
});
test('transport rejects after withdrawal and unknown body types',()=>{
  const f=collector();f.navigator.sendBeacon(request(),{});assert.equal(f.sends.length,0);
  f.parent['ga-disable-'+id]=true;f.navigator.sendBeacon(request());assert.equal(f.sends.length,0);
});
test('fetch rejects blocked events without fabricating HTTP success',async()=>{
  const f=collector();await assert.rejects(f.window.fetch(request({en:'form_submit'})),/Measurement request blocked/);
  assert.equal(f.sends.length,0);
  await f.window.fetch(request());assert.equal(f.sends.length,1);
  f.navigator.sendBeacon(request());assert.equal(f.sends.length,1);
});
test('XHR aborts blocked events and permits the real pageview once',()=>{
  const f=collector();const bad=new f.XMLHttpRequest();bad.open('POST',request({en:'scroll'}));bad.send();
  assert.equal(bad.aborted,true);assert.equal(f.sends.length,0);
  const good=new f.XMLHttpRequest();good.open('POST',request());good.send();assert.equal(f.sends.length,1);
});
test('blocked beacon reports failure instead of accepted delivery',()=>{
  const f=collector();assert.equal(f.navigator.sendBeacon(request({en:'scroll'})),false);
  assert.equal(f.sends.length,0);
});
test('another public pathname or title cannot replace the actual document view',()=>{
  const f=collector();f.navigator.sendBeacon(request({dl:'https://www.wahojobs.com/jobs/opportunity-123'}));
  f.navigator.sendBeacon(request({dt:'Job opportunity | Wahojobs'}));assert.equal(f.sends.length,0);
});
for(const body of [null,'dl=https%3A%2F%2Fwww.wahojobs.com%2Fjobs'])
  test('duplicate parameters cannot hide a different URL/body value '+String(body),()=>{
    const f=collector();const duplicate=body===null ? request()+'&dl=private%40example.test' : request();
    f.navigator.sendBeacon(duplicate,body);assert.equal(f.sends.length,0);
  });
for(const prefix of ['https://www.google-analytics.com:444','https://name:secret@www.google-analytics.com'])
  test('transport rejects credentials and nonstandard origins '+prefix,()=>{
    const f=collector();f.navigator.sendBeacon(request().replace('https://www.google-analytics.com',prefix));
    assert.equal(f.sends.length,0);
  });
