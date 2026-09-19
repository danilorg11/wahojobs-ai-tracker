'use strict';
const assert=require('node:assert/strict');
module.exports=function exerciseEducation(w){
 const d=w.document,form=d.querySelector('.candidate-correction'),section=d.querySelector('#section-education');
 const holder=section.querySelector('[data-education-editor]'),items=holder.querySelector('[data-education-items]');
 const q=s=>{const el=section.querySelector(s);assert.ok(el,s);return el;};
 const set=(el,value)=>{el.value=value;el.dispatchEvent(new w.Event('input',{bubbles:true}));el.dispatchEvent(new w.Event('change',{bubbles:true}));};
 // This fixture's original beginner profile explicitly says it has no degree.
 // The candidate updates those declarations when adding a completed PhD.
 if(q('[name=no_degree]').checked)q('[name=no_degree]').click();
 if(q('[name=education_level]').value==='no_degree')set(q('[name=education_level]'),'not_specified');
 const constraints=d.querySelector('[name=hard_constraints]');
 set(constraints,constraints.value.split(',').map(v=>v.trim()).filter(v=>v!=='no college degree').join(', '));
 const fields=()=>Object.fromEntries(new w.FormData(form)),undo=()=>q('[data-undo-education]');
 const active=()=>[...items.querySelectorAll('[data-education-entry]')].filter(e=>!e.hidden);
 const add=values=>{q('[data-add-education]').click();const row=active().at(-1);for(const [key,value] of Object.entries(values))set(row.querySelector('[data-education-key='+key+']'),value);return row;};
 const first=add({qualification:'Additional study',field:'Ecology',institution:'Example School'});
 const second=add({qualification:'Additional study',field:'Genetics'});
 const phd=add({kind:'phd',qualification:'Biology',field:'Biology',institution:'U Penn',status:'completed'});
 assert.match(phd.querySelector('summary').textContent,/PhD in Biology/);
 assert.equal((phd.querySelector('summary').textContent.match(/Biology/g)||[]).length,1);
 set(phd.querySelector('[data-education-key=qualification]'),'PhD in Biology');
 assert.doesNotMatch(phd.querySelector('summary').textContent,/PhD.*PhD/);
 set(phd.querySelector('[data-education-key=qualification]'),'Biology');
 const legacy=[];
 for(const name of ['degrees','education_fields']){
  const group=q('[data-chips='+name+']');
  for(const value of ['First '+name,'Second '+name]){
   group.querySelector('[data-add-chip]').click();
   const row=[...group.querySelectorAll('[data-collection-item]')].at(-1);
   set(row.querySelector('input:not([type=checkbox])'),value);legacy.push(row);
  }
 }
 // Keep original nodes, ordering, entered values and native validation state.
 const year=first.querySelector('[data-education-key=completion_year]'),kind=first.querySelector('select');
 set(year,'20');kind.disabled=true;
 const baseline=fields(),nodeOrder=[...items.children],chipOrders=[...section.querySelectorAll('[data-chip-items]')].map(el=>[el,[...el.children]]);
 const removals=[first,second,...legacy];
 for(const row of removals){
  row.querySelector('[data-remove-education],[data-collection-remove-action]').click();
  assert.ok(row.hidden);assert.equal(d.activeElement,undo());
  assert.equal(section.querySelectorAll('[data-education-undo]:not([hidden])').length,1);
  assert.equal(section.querySelectorAll('[data-undo-item],[data-undo-entry]').length,0);
 }
 assert.equal(year.willValidate,false);assert.ok(kind.disabled);
 assert.match(undo().getAttribute('aria-label'),/Second education_fields/);
 set(d.querySelector('[name=city]'),'Unrelated unsaved city');
 undo().click();assert.equal(legacy.at(-1).hidden,false);
 legacy.at(-1).querySelector('[data-collection-remove-action]').click();
 for(const row of [...removals].reverse()){
  section.open=false; // Restoring must reopen collapsed ancestors and focus a usable control.
  undo().click();assert.equal(row.hidden,false);assert.ok(section.open);
  assert.ok(row.contains(d.activeElement));assert.equal(d.activeElement.disabled,false);
 }
 assert.equal(q('[data-education-undo]').hidden,true);
 assert.deepEqual([...items.children],nodeOrder);
 for(const [parent,children] of chipOrders)assert.deepEqual([...parent.children],children);
 assert.deepEqual(fields(),{...baseline,city:'Unrelated unsaved city'});
 assert.equal(year.willValidate,true);assert.equal(year.checkValidity(),false);assert.ok(kind.disabled);
 set(year,'2020');kind.disabled=false;
 // Leave only the intended removals in the real review submission.
 second.querySelector('[data-remove-education]').click();
 legacy[0].querySelector('[data-collection-remove-action]').click();
 legacy[2].querySelector('[data-collection-remove-action]').click();
 const posted=[...new w.FormData(form)],remaining=JSON.parse(fields().education_entries);
 assert.equal(posted.filter(([name])=>name==='education_entries').length,1);
 assert.equal(remaining.filter(e=>e.kind==='phd'&&e.field==='Biology').length,1);
 assert.ok(!remaining.some(e=>e.field==='Genetics'));
 assert.ok(remaining.some(e=>e.field==='Ecology'&&e.completion_year===2020));
 assert.ok(!fields().degrees.includes('First degrees'));assert.ok(fields().degrees.includes('Second degrees'));
 assert.ok(!fields().education_fields.includes('First education_fields'));assert.ok(fields().education_fields.includes('Second education_fields'));
 assert.equal(form.checkValidity(),true);
 return {remaining,fields:posted};
};
