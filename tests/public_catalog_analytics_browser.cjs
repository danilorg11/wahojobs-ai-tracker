// Opt-in Edge acceptance. Fulfill anonymous documents and a public gtag snapshot;
// intercept every collection request. No analytics receipt or production write.
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const [fixturePath, tagPath, outputPath] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const googleTag = fs.readFileSync(tagPath, 'utf8');
const gatewayPath = process.argv[5];
const cmpPath = process.argv[6];
const origin = 'https://www.wahojobs.com';
const id = 'G-QFMW1WX907';
const decision = granted => ({cmpStatus:'loaded', gdprApplies:true,
  eventStatus:'useractioncomplete', purpose:{consents:{1:granted}},
  vendor:{consents:{755:granted}}});
(async () => {
  const browser = await chromium.launch({channel:'msedge', headless:true});
  const gateway = gatewayPath ? (await import(require('node:url').pathToFileURL(gatewayPath).href)).catalogGateway : null;
  const results = [];
  try {
    for (const mode of ['grant', 'deny', 'pending', 'late-grant', 'non-tcf', ...(cmpPath ? ['real-regional'] : [])]) {
      const context = await browser.newContext();
      const collected = [], errors = [], violations = [];
      let googleLoads = 0;
      await context.addInitScript(({mode, first}) => {
        window.__nativeProductTransport={fetch:window.fetch,beacon:navigator.sendBeacon,
          open:XMLHttpRequest.prototype.open,send:XMLHttpRequest.prototype.send};
        window.__fixtureDecision = first;
        window.__fixtureMode = mode;
        document.addEventListener('securitypolicyviolation', e => {
          (window.__violations ||= []).push({directive:e.violatedDirective, uri:e.blockedURI});
        });
      }, {mode, first:decision(mode==='grant')});
      await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (url.origin === origin) {
          if (request.resourceType() === 'document') {
            const isFrame=url.pathname==='/jobs/_analytics';
            const body=isFrame ? fixture.frame : url.pathname==='/jobs' ? fixture.catalog : fixture.detail;
            const csp=isFrame ? fixture.frameCsp : fixture.csp;
            if (gateway) {
              const response=await gateway(new Request(url.href,{headers:{host:url.host}}),{
                WAHOJOBS_CATALOG_ENABLED:'1',WAHOJOBS_CATALOG_INDEXABLE:'1',
                WAHOJOBS_CATALOG_KEY:'a'.repeat(64),VERCEL_ENV:'production'
              },async()=>new Response(body,{headers:{'content-type':'text/html; charset=utf-8',
                'x-wahojobs-catalog-robots':isFrame?'noindex,follow':'index,follow'}}));
              assert.equal(response.headers.get('content-security-policy'),csp);
              return route.fulfill({status:response.status,headers:Object.fromEntries(response.headers),body:await response.text()});
            }
            return route.fulfill({status:200, contentType:'text/html',
              headers:{'Content-Security-Policy':csp},body});
          }
          return route.abort();
        }
        if (url.hostname === 'the.gatekeeperconsent.com') {
          if (mode==='real-regional') {
            if(url.pathname==='/cmp.min.js')return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(cmpPath,'utf8')});
            return request.method()==='GET'?route.continue():route.abort();
          }
          return route.fulfill({contentType:'text/javascript', body:`
            window.ezTcfConsent={loaded:true,store_info:true};
            window.ezCMPQueue={gotResponse:true,push:function(cb){cb();}};
            if(window.__fixtureMode!=='non-tcf') window.__tcfapi=function(c,v,cb){
              window.__notify=cb;
              if(window.__fixtureMode!=='pending') cb(window.__fixtureDecision,true);
            };`});
        }
        if (mode==='real-regional' && ['privacy.gatekeeperconsent.com','gvl.gatekeeperconsent.com'].includes(url.hostname))
          return request.method()==='GET'?route.continue():route.abort();
        if (url.hostname==='www.googletagmanager.com' && url.pathname==='/gtag/js') {
          googleLoads++;
          return route.fulfill({contentType:'text/javascript',body:googleTag});
        }
        if (url.pathname.endsWith('/g/collect')) {
          const pairs = new URLSearchParams(url.search);
          for (const [key,value] of new URLSearchParams(request.postData() || '')) pairs.set(key,value);
          const decoded = [...pairs.values()].join(' ');
          if (/fixture-secret|private@example|email=|token=|otp=|return_to=|variant=/.test(decoded))
            errors.push('Sensitive synthetic value reached collector');
          collected.push({event:pairs.get('en'),tid:pairs.get('tid'),
            location:pairs.get('dl'),referrer:pairs.get('dr'),title:pairs.get('dt')});
          return route.fulfill({status:204, body:''});
        }
        return route.abort();
      });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(origin+'/jobs?q=private%40example.test&location=Brazil#otp=fixture-secret',
        {referer:origin+'/candidate/auth/callback?code=fixture-secret'});
      if (mode==='late-grant') {
        await page.waitForTimeout(250);
        assert.equal(googleLoads,0);
        await page.evaluate(data => window.__notify(data,true), decision(true));
      }
      await page.waitForTimeout(1500);
      if (['grant','late-grant','non-tcf','real-regional'].includes(mode)) {
        for (let attempt=0;attempt<40 && !collected.length;attempt++) await page.waitForTimeout(100);
      }
      if (['grant','late-grant','non-tcf','real-regional'].includes(mode) && !collected.length) {
        console.log(JSON.stringify({mode,googleLoads,errors,
          frames:await Promise.all(page.frames().map(f=>f.evaluate(()=>({
            href:location.href,origin:location.origin,
            dataLayer:(window.dataLayer||[]).map(c=>Array.from(c)).slice(0,10),
            violations:window.__violations||[],cookies:document.cookie.split(';').map(c=>c.split('=')[0])
          })).catch(e=>({error:e.message}))))}));
      }
      if (['grant','late-grant','non-tcf','real-regional'].includes(mode)) {
        assert.equal(googleLoads,1);
        assert.equal(collected.filter(e=>e.event==='page_view').length,1,JSON.stringify(collected));
        assert.equal(collected[0].tid,id);
        assert.equal(collected[0].location,origin+'/jobs');
        assert(!collected[0].referrer);
        assert((await context.cookies()).some(c=>c.name==='_ga'));
        if (mode==='grant') {
          await page.evaluate(data => window.__notify(data,true), decision(true));
          await page.evaluate(source => (0,eval)(source),fixture.script);
          await page.waitForTimeout(250);
          assert.equal(googleLoads,1);
          assert.equal(collected.filter(e=>e.event==='page_view').length,1);
          await page.evaluate(data => window.__notify(data,true), decision(false));
          assert.equal(await page.evaluate(id=>window['ga-disable-'+id],id),true);
          await page.goto(origin+'/jobs/opportunity-9002?variant=9003&return_to=%2Fjobs');
          await page.waitForTimeout(1500);
          assert.equal(collected.filter(e=>e.event==='page_view').length,2,JSON.stringify(collected));
          assert.equal(collected.at(-1).location,origin+'/jobs/opportunity-9002');
        }
      } else {
        assert.equal(googleLoads,0); assert.equal(collected.length,0);
        assert(!(await context.cookies()).some(c=>c.name.startsWith('_ga')));
      }
      violations.push(...await page.evaluate(()=>window.__violations||[]));
      assert(await page.evaluate(()=>{
        const original=window.__nativeProductTransport;
        return original.fetch===window.fetch && original.beacon===navigator.sendBeacon &&
          original.open===XMLHttpRequest.prototype.open && original.send===XMLHttpRequest.prototype.send;
      }),'product transport must remain unchanged');
      assert(collected.every(e=>e.event==='page_view'),JSON.stringify(collected));
      assert.deepEqual(errors,[]); assert.deepEqual(violations,[]);
      results.push({mode,googleLoads,collected,errors,violations,collectionIntercepted:true});
      await context.close();
    }
  } finally {
    await browser.close();
    fs.writeFileSync(outputPath,JSON.stringify({results,receiptVerified:false},null,2));
  }
  console.log(JSON.stringify({passed:results.length,modes:results.map(r=>r.mode),collectionIntercepted:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
