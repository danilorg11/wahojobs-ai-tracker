// Anonymous disposable HTTPS acceptance. No external destinations are allowed.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async()=>{
 const [origin,out] = process.argv.slice(2);fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:900}});
 const external=[];await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin!==origin){external.push(url.origin);return route.abort();}return route.continue();});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 async function clickNav(locator){await Promise.all([page.waitForLoadState('domcontentloaded'),locator.click()]);}
 async function editor(){await page.goto(origin+'/account/profile?correction=start');await page.getByRole('button',{name:'Update profile',exact:true}).click();await page.getByRole('link',{name:'Edit profile',exact:true}).click();await page.locator('#section-skills > summary').click();}
 try{
  await page.goto(origin+'/login?next=/account/profile');await page.locator('form[action="/auth/google/start"] button').click();await page.getByRole('link',{name:'Approve fixture login'}).click();await page.waitForURL('**/account/profile');
  await page.goto(origin+'/find-matches');const runId=await page.locator('a').evaluateAll(a=>a.map(x=>new URL(x.href).searchParams.get('run')).find(Boolean));const oldRun=runId?'/find-matches?run='+runId:null;
  assert(oldRun,'normal result context available');
  await editor();
  const row=page.locator('#skills [data-collection-item]').filter({has:page.locator('input[value="R"]')});
  await row.getByRole('button',{name:'Add experience details',exact:true}).click();
  const dialog=page.getByRole('dialog');assert(await dialog.isVisible());
  await dialog.locator('[data-item-context][value=professional]').check();await dialog.locator('[data-item-months]').fill('42');
  await dialog.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal(await row.locator('[data-item-summary]').innerText(),'');
  await row.getByRole('button',{name:'Add experience details',exact:true}).focus();await page.keyboard.press('Enter');
  assert.equal(await dialog.locator('[data-item-months]').inputValue(),'');
  await dialog.locator('[data-item-context][value=study]').check();await dialog.locator('[data-item-context][value=projects]').check();
  await dialog.locator('[data-item-autonomy]').selectOption('guided');await dialog.locator('[data-item-months]').fill('6');
  await dialog.getByRole('button',{name:'Save details to draft'}).click();
  const hidden=page.locator('input[name=item_experience]');const original=JSON.parse(await hidden.inputValue())[0];assert.equal(original.months,6);
  const input=row.locator('input:not([type=checkbox])').first();await input.fill('R programming');assert.equal(JSON.parse(await hidden.inputValue())[0].item_id,original.item_id);await input.fill('R');
  await row.locator('[data-collection-remove]').check();assert.deepEqual(JSON.parse(await hidden.inputValue()),[]);
  await page.getByRole('button',{name:'Undo removal: R',exact:true}).click();assert.deepEqual(JSON.parse(await hidden.inputValue()),[original]);
  // Normal keyboard access and a mobile layout, on the same disposable owner.
  await page.setViewportSize({width:390,height:844});await row.getByRole('button',{name:'Edit experience details'}).click();
  assert(await dialog.isVisible());await dialog.locator('[data-item-months]').focus();await page.keyboard.press('Tab');
  assert(await dialog.evaluate(d=>d.contains(document.activeElement)));
  const dimensions=await dialog.evaluate(d=>({scroll:d.scrollWidth,width:d.clientWidth,rect:d.getBoundingClientRect().width}));assert(dimensions.scroll<=dimensions.width+1);assert(dimensions.rect<=390);
  await page.screenshot({path:path.join(out,'mobile-experience-dialog.png')});await page.keyboard.press('Escape');
  await page.locator('input[name=credentials_confirmed]').check();await page.getByRole('button',{name:'Review changes',exact:true}).scrollIntoViewIfNeeded();await page.getByRole('button',{name:'Review changes',exact:true}).click();
  await page.getByRole('heading',{name:'Confirm this correction'}).waitFor();assert((await page.locator('body').innerText()).includes('about 6 months'));
  await page.reload();assert((await page.locator('body').innerText()).includes('about 6 months'));
  await page.setViewportSize({width:1440,height:900});await page.screenshot({path:path.join(out,'desktop-experience-review.png'),fullPage:true});
  await page.locator('input[name=confirmed]').check();await page.getByRole('button',{name:'Apply profile update',exact:true}).click();await page.getByRole('heading',{name:'Profile changes saved',exact:true}).waitFor();
  await page.goto(origin+oldRun);assert.equal((await page.title()).includes('Error'),false);
  const detail=page.locator('a[href^="/job/"]').first();assert(await detail.count());const destination=await detail.getAttribute('href');assert(destination.includes('variant=7003'));if(!await detail.isVisible())await page.getByText('Possibilities with conditions to check',{exact:true}).click();await detail.click();
  const detailText=await page.locator('body').innerText();assert(detailText.includes('You report use of R'));assert(detailText.includes('requested proficiency')||detailText.includes('still aren’t established'));
  assert(detailText.includes('self')||detailText.includes('own assessment'));
  assert.equal(external.length,0);assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'browser-results.json'),JSON.stringify({passed:true,desktop:[1440,900],mobile:[390,844],normalLogin:true,saveCancel:true,renameDeleteUndo:true,reloadReview:true,explicitApply:true,oldRun:true,exactVariant:7003,externalRequests:external.length,jsErrors:errors}));
 }catch(error){fs.writeFileSync(path.join(out,'failed-page.html'),await page.content());await page.screenshot({path:path.join(out,'failed-page.png'),fullPage:true});throw error;}finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
