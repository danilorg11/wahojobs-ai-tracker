'use strict';
// Runs the shipped scripts against supplied synthetic HTML. No network transport.
const assert = require('node:assert/strict');
const {JSDOM, VirtualConsole} = require('jsdom');
let source = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => source += chunk);
process.stdin.on('end', async () => {
  try {
    const input = JSON.parse(source), errors = [];
    const console = new VirtualConsole();
    console.on('jsdomError', e => errors.push(String(e)));
    const dom = new JSDOM(input.html, {url: 'https://localhost:1/account/profile', runScripts: 'dangerously', virtualConsole: console,
      beforeParse(w) {
        w.HTMLElement.prototype.scrollIntoView = function() {};
        w.HTMLDialogElement.prototype.showModal = function() {this.open = true;};
        w.HTMLDialogElement.prototype.close = function() {this.open = false; this.dispatchEvent(new w.Event('close'));};
      }});
    const w = dom.window, d = w.document, form = d.querySelector('.candidate-correction');
    const q = s => {const e=d.querySelector(s); assert.ok(e, s); return e;};
    const fields = () => Object.fromEntries(new w.FormData(form).entries());
    const set = (element, value) => {element.value=value; element.dispatchEvent(new w.Event('input',{bubbles:true}));element.dispatchEvent(new w.Event('change',{bubbles:true}));};
    const summary = id => q('#section-'+id+' [data-section-summary]').textContent;
    const visible = selector => [...d.querySelectorAll(selector)].filter(e=>!e.hidden);
    assert.equal(errors.length, 0, errors.join('\n'));
    assert.equal(q('[data-local-edit-status]').textContent, '');
    if (input.scenario === 'languages') {
      set(q('[name=language_0]'), 'Portuguese');set(q('[name=language_proficiency_0]'),'native');
      if(q('[name=language_1]').closest('[data-language-row]').hidden)q('[data-add-language]').click();
      set(q('[name=language_1]'), 'English');set(q('[name=language_proficiency_1]'),'fluent');
      q('#section-languages').open=true;
      q('#section-languages [data-section-done]').click();
      assert.match(summary('languages'), /Portuguese \(Native\)/);
      assert.match(summary('languages'), /English \(Fluent\)/);
      assert.equal(q('#section-languages').open, false);
      assert.equal(d.activeElement, q('#section-languages > summary'));
      assert.match(q('[data-local-edit-status]').textContent,/not saved yet/);
      q('#section-languages').open=true;
      assert.equal(q('[name=language_proficiency_0]').value,'native');
      assert.equal(q('[name=language_proficiency_1]').value,'fluent');
      const before=fields(), count=visible('[data-language-row]').length;
      assert.equal(d.querySelectorAll('[data-add-language]').length,1);
      q('[data-add-language]').click();q('[data-add-language]').click();
      assert.equal(visible('[data-language-row]').length,count+1);
      assert.deepEqual(fields(),before, 'revealing an empty field is not a fact');
      const first=q('[name=language_0]').closest('[data-language-row]');
      first.querySelector('[data-remove-language]').click();
      assert.equal(fields().language_0,'');assert.equal(fields().language_proficiency_0,'unspecified');
      assert.doesNotMatch(summary('languages'),/Portuguese/);
      assert.equal(d.activeElement,q('[data-undo-language]'));
      q('[data-undo-language]').click();assert.deepEqual(fields(),before);
      assert.equal(d.activeElement,q('[name=language_0]'));
      if(input.invalidCountry)set(q('[name=country]'),'Unknown synthetic location');
    } else if (input.scenario === 'rows') {
      const roles=q('[data-chips=job_titles]');
      if(!roles.querySelector('[data-collection-item]')){roles.querySelector('[data-add-chip]').click();set(roles.querySelector('input:not([type=checkbox])'),'Customer support representative');}
      assert.equal(roles.querySelector('label span').textContent,'Role');
      assert.ok(roles.querySelector('[data-collection-remove]').hidden);
      assert.equal(roles.querySelector('[data-collection-remove]').name,'');
      const before=fields(), row=roles.querySelector('[data-collection-item]');
      row.querySelector('[data-collection-remove-action]').click();assert.ok(row.hidden);
      assert.equal(d.activeElement,q('#section-experience [data-undo-section]'));
      assert.equal(d.querySelectorAll('#section-experience [data-undo-section]').length,1);
      q('#section-experience [data-undo-section]').click();assert.deepEqual(fields(),before);
      assert.equal(d.activeElement,row.querySelector('input:not([type=checkbox])'));
      const count=roles.querySelectorAll('[data-collection-item]').length;
      roles.querySelector('[data-add-chip]').click();roles.querySelector('[data-add-chip]').click();
      assert.equal(roles.querySelectorAll('[data-collection-item]').length,count+1);
      assert.deepEqual(fields(),before);
      for (const kind of ['employment','education']) {
        const holder=q('[data-'+kind+'-editor]'), add=holder.querySelector('[data-add-'+kind+']');
        const key=kind==='employment'?'recent_roles':'education_entries', original=fields()[key];
        const count=visible('[data-'+kind+'-entry]').length;
        add.click();add.click();assert.equal(visible('[data-'+kind+'-entry]').length,count+1);
        assert.deepEqual(JSON.parse(fields()[key]),JSON.parse(original),'empty '+kind+' row must not become a fact');
        const entry=holder.querySelector('[data-'+kind+'-entry]');
        let invalidYear;
        if(kind==='education'){invalidYear=entry.querySelector('[data-education-key=completion_year]');set(invalidYear,'20');assert.equal(invalidYear.checkValidity(),false);}
        entry.querySelector('[data-remove-'+kind+']').click();assert.ok(entry.hidden);
        if(invalidYear){assert.equal(invalidYear.willValidate,false);assert.equal(invalidYear.disabled,true);}
        (kind==='education'?q('#section-education [data-undo-education]'):q('#section-experience [data-undo-section]')).click();assert.ok(!entry.hidden);
        if(invalidYear){assert.equal(invalidYear.disabled,false);assert.equal(invalidYear.checkValidity(),false);set(invalidYear,'');}
        assert.deepEqual(JSON.parse(fields()[key]),JSON.parse(original));
      }
    } else if (input.scenario === 'work-simple') {
      const before=fields(), legacy=q('[data-employment-entry]'), original=legacy.querySelector('[data-employment-value]').value;
      assert.match(original,/Example Company/);
      assert.equal(d.querySelectorAll('#section-skills [data-chips] h3').length,0);
      assert.equal([...d.querySelectorAll('#section-skills [data-add-chip]')].filter(e=>!e.hidden).length,1);
      assert.ok(q('#section-skills').contains(q('[data-chips=specialties]')));
      assert.match(q('#ability-examples').textContent,/Python.*Excel.*proofreading/);
      assert.doesNotMatch(form.textContent,/Role, employer and dates|Add employment detail|Add experience details/);
      q('[data-add-employment]').click();q('[data-add-employment]').click();
      assert.deepEqual(fields(),before,'Blank job is not a new fact');
      const row=[...d.querySelectorAll('[data-employment-entry]')].at(-1), control=k=>row.querySelector('[data-work-key='+k+']');
      assert.equal(d.activeElement,control('title'));
      set(control('title'),'Reviewer');set(control('organization'),'Example, Inc.');
      set(control('start'),'2021');set(control('end'),'2020');
      assert.equal(control('title').checkValidity(),false);set(control('end'),'2024');
      assert.equal(control('title').checkValidity(),true);
      set(control('start'),'20');assert.equal(control('start').checkValidity(),false);set(control('start'),'2021');
      set(control('organization'),'x'.repeat(125));assert.equal(control('title').checkValidity(),false);
      set(control('organization'),'Example, Inc.');
      row.querySelector('[data-work-current]').click();assert.equal(control('end').disabled,true);
      assert.equal(control('end').closest('label').hidden,true);
      row.querySelector('[data-work-current]').click();assert.equal(control('end').value,'2024');assert.equal(control('end').disabled,false);
      assert.equal(control('end').closest('label').hidden,false);
      row.querySelector('[data-work-current]').click();
      const expected='Title: Reviewer | At: Example, Inc. | From: 2021 | To: Present';
      assert.deepEqual(JSON.parse(fields().recent_roles),[original,expected]);
      set(q('[name=city]'),'Unrelated work edit');
      const role=d.querySelector('[data-chips=job_titles] [data-collection-item]');
      legacy.querySelector('[data-remove-employment]').click();row.querySelector('[data-remove-employment]').click();
      assert.doesNotMatch(q('#section-experience [data-section-undo]').textContent,/Title:|From:|To:|At:/);
      if(role)role.querySelector('[data-collection-remove-action]').click();
      assert.equal(d.querySelectorAll('#section-experience [data-undo-section]').length,1);
      if(role)q('#section-experience [data-undo-section]').click();
      q('#section-experience [data-undo-section]').click();assert.equal(d.activeElement,control('title'));
      assert.equal(control('end').disabled,true);assert.equal(control('end').value,'2024');
      q('#section-experience [data-undo-section]').click();assert.equal(d.activeElement,legacy.querySelector('textarea'));
      assert.deepEqual(JSON.parse(fields().recent_roles),[original,expected]);
      assert.equal(fields().city,'Unrelated work edit');
      const tasks=[...d.querySelectorAll('#section-skills [data-collection-item]')],beforeAbilities=fields();
      tasks.forEach(r=>r.querySelector('[data-collection-remove-action]').click());
      assert.equal(d.querySelectorAll('#section-skills [data-undo-section]').length,1);
      tasks.forEach(()=>q('#section-skills [data-undo-section]').click());assert.deepEqual(fields(),beforeAbilities);
      set(q('[name=no_experience]'),'');q('[name=no_experience]').checked=false;
      assert.equal(q('[name=recent_roles]').value.split(expected).length,2,'One submitted copy of the job');
      q('#section-experience [data-section-done]').click();assert.match(summary('experience'),/Reviewer · Example, Inc. · 2021–Present/);
      for(const key of ['specialties','skills','software_tools','professional_domains'])assert.equal(fields()[key],before[key],key+' keeps its source field');
      q('#skills [data-add-chip]').click();
      set([...d.querySelectorAll('#skills [data-collection-item] input:not([type=checkbox])')].at(-1),'Spreadsheet formulas');
    } else if (input.scenario === 'work-reopen') {
      const before=fields(), row=q('[data-work-key=title]').closest('[data-employment-entry]');
      assert.equal(row.querySelector('[data-work-key=title]').value,'Reviewer');
      assert.equal(row.querySelector('[data-work-key=organization]').value,'Example, Inc.');
      assert.equal(row.querySelector('[data-work-key=start]').value,'2021');
      assert.ok(row.querySelector('[data-work-current]').checked);
      row.querySelector('[data-work-current]').click();assert.equal(row.querySelector('[data-work-key=end]').disabled,false);
      row.querySelector('[data-work-current]').click();assert.deepEqual(fields(),before);
      assert.equal(q('[data-local-edit-status]').textContent,'');
      assert.equal(q('[data-employment-editor]')._summary('Title: Reviewer | From: 2021'),'Reviewer · Started 2021');
    } else if (input.scenario === 'education') {
      require('./education_editor_exercise.cjs')(w);
    } else if (input.scenario === 'education-capacity') {
      const holder=q('[data-education-editor]'),items=holder.querySelector('[data-education-items]'),add=q('[data-add-education]');
      const active=()=>[...items.querySelectorAll('[data-education-entry]')].filter(e=>!e.hidden);
      while(active().length<24){add.click();set(active().at(-1).querySelector('[data-education-key=field]'),'Capacity topic '+active().length);}
      const original=active()[0];original.querySelector('[data-remove-education]').click();
      add.click();set(active().at(-1).querySelector('[data-education-key=field]'),'Replacement topic');
      q('[data-undo-education]').click();
      assert.equal(active().length,25);assert.ok(!original.hidden);
      assert.equal(q('[data-education-limit]').hidden,false);
      assert.equal(JSON.parse(fields().education_entries).length,25,'Undo retains every entered fact even at the limit');
    } else if (input.scenario === 'education-group') {
      const group=q('[data-study-group]'),rows=[...group.querySelectorAll('[data-education-entry]')];
      rows.forEach(row=>row.querySelector('[data-remove-education]').click());
      assert.ok(group.hidden);assert.equal(q('[data-education-undo]').hidden,false);
      q('[data-undo-education]').click();assert.equal(group.hidden,false);assert.ok(group.open);
      assert.ok(rows.at(-1).contains(d.activeElement));
    } else if (input.scenario === 'item') {
      const row=q('#skills [data-collection-item]'), opener=row.querySelector('[data-experience-open]');
      const before=fields().item_experience;
      opener.click();q('[data-item-months]').value='24';q('[data-item-cancel]').click();
      assert.equal(fields().item_experience,before);assert.equal(d.activeElement,opener);
      opener.click();q('[data-item-months]').value='24';q('[data-item-save]').click();
      const saved=fields().item_experience;assert.equal(JSON.parse(saved)[0].months,24);
      row.querySelector('[data-collection-remove-action]').click();assert.equal(fields().item_experience,'[]');
      assert.equal(d.querySelectorAll('#section-skills [data-undo-section]').length,1);
      q('#section-skills [data-undo-section]').click();assert.equal(fields().item_experience,saved);
      opener.click();q('[data-item-clear]').click();assert.equal(fields().item_experience,'[]');
    } else if (input.scenario === 'duration') {
      const holder=q('[data-domain-duration-editor]');
      const row=holder.querySelector('[data-domain="customer support"]'), control=row.querySelector('input');
      const before=fields(), original=JSON.parse(before.domain_years_review);
      assert.equal(control.value,'2');
      set(control,'3');q('#section-experience [data-section-done]').click();
      assert.match(summary('experience'),/customer support: 3 years/);
      assert.equal(fields().total_years,before.total_years);
      row.querySelector('[data-remove-domain-duration]').click();
      assert.ok(!JSON.parse(fields().domain_years_review).entries.some(e=>e.domain==='customer support'));
      holder.querySelector('[data-undo-domain-duration]').click();assert.equal(control.value,'3');assert.equal(d.activeElement,control);
      assert.equal(JSON.parse(fields().domain_years_review).entries.find(e=>e.domain==='writing').years,
        original.entries.find(e=>e.domain==='writing').years);
      if(input.clearDuration)row.querySelector('[data-remove-domain-duration]').click();
      if(input.blankDuration)set(control,'');
    } else if (input.scenario === 'manual-status') {
      set(q('[name=city]'),'Synthetic city');
      assert.equal(q('[data-local-edit-status]').textContent,'','manual autosave owns draft status');
    } else if (input.scenario === 'restored') {
      assert.match(summary('languages'),/Portuguese \(Native\)/);
      assert.match(summary('languages'),/English \(Fluent\)/);
      assert.equal(q('[name=language_proficiency_0]').value,'native');
      assert.equal(q('[name=language_proficiency_1]').value,'fluent');
      assert.equal(d.activeElement,q('[name=country]'));
    } else throw new Error('Unknown fixture scenario');
    await new Promise(resolve=>w.setTimeout(resolve,1));
    assert.equal(errors.length,0,errors.join('\n'));
    if(d.querySelector('[name=credentials_confirmed]'))q('[name=credentials_confirmed]').checked=true;
    process.stdout.write(JSON.stringify({fields:[...new w.FormData(form).entries()],action:form.getAttribute('action'),languages:summary('languages')}));
    dom.window.close();
  } catch(e) {process.stderr.write(e.stack+'\n');process.exitCode=1;}
});
