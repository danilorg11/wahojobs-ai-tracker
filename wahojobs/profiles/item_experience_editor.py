"""One optional dialog reused by the existing skill/activity chips."""
from html import escape
from wahojobs.profiles.item_experience import CONTEXTS, AUTONOMY


def dialog():
    short = {'unknown': 'Not specified', 'guided': 'With guidance',
             'independent': 'Independent routine tasks', 'complex': 'Independent complex tasks'}
    return ("<dialog id='item-experience-dialog' aria-labelledby='item-experience-title'>"
        "<h2 id='item-experience-title'>Experience details</h2><p data-item-label></p>"
        "<p>Optional, self-reported information. Nothing is confirmed until you review and apply your profile update.</p>"
        "<fieldset><legend>Where have you used it?</legend>"
        + ''.join(f"<label class='item-context'><input type='checkbox' data-item-context value='{key}'> {escape(label)}</label>" for key,label in CONTEXTS.items())
        + "</fieldset><label class='review-field'><span>How do you use it?</span><select data-item-autonomy>"
        + ''.join(f"<option value='{key}' data-description='{escape(label, quote=True)}'>{short[key]}</option>" for key,label in AUTONOMY.items())
        + "</select></label><p data-autonomy-description></p><label class='review-field'><span>Approximate months using this item</span>"
        "<input data-item-months type='number' min='0' max='960' step='1' inputmode='numeric' aria-describedby='item-duration-note' disabled></label>"
        "<p id='item-duration-note'>For this item only, not your total career. Leave blank if unknown; use 0 for less than a month.</p>"
        "<p data-item-error role='alert' hidden></p><div class='item-dialog-actions'>"
        "<button type='button' data-item-save>Save details to draft</button>"
        "<button type='button' class='button-quiet' data-item-cancel>Cancel</button>"
        "<button type='button' class='button-quiet' data-item-clear>Clear experience details</button>"
        "</div></dialog>")


STYLE = """
#item-experience-dialog{box-sizing:border-box;border:1px solid #cbd5e1;border-radius:16px;padding:24px;width:min(560px,calc(100vw - 24px));max-height:90vh;overflow:auto;color:#172d42}
#item-experience-dialog::backdrop{background:rgba(15,23,42,.4)}
#item-experience-dialog select,#item-experience-dialog input[type=number]{width:100%;max-width:100%;box-sizing:border-box;min-height:44px}
#item-experience-dialog .item-context{display:flex;align-items:center;gap:8px;min-height:44px}
.item-dialog-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.item-dialog-actions button{min-height:44px}
.candidate-correction [data-experience-open]{font-size:.85rem;min-height:36px;margin-top:4px}
.candidate-correction [data-item-summary]{font-size:.85rem;max-width:38rem;overflow-wrap:anywhere;margin:4px 0}
.candidate-correction [data-undo-item]{font-size:.85rem;min-height:44px}
@media(max-width:700px){#item-experience-dialog{padding:16px}.item-dialog-actions{display:grid}.item-dialog-actions button{width:100%}}
"""

SCRIPT = r"""
(function(){'use strict';var form=document.querySelector('.candidate-correction');if(!form)return;
var hidden=form.querySelector('[name=item_experience]'),dialog=document.getElementById('item-experience-dialog');if(!hidden||!dialog)return;
var records=JSON.parse(hidden.value||'[]'),current=null,opener=null;
function autonomyDescription(){var select=dialog.querySelector('[data-item-autonomy]'),note=dialog.querySelector('[data-autonomy-description]');note.textContent=select.value==='unknown'?'':select.selectedOptions[0].dataset.description;note.hidden=!note.textContent;}
dialog.querySelector('[data-item-autonomy]').addEventListener('change',autonomyDescription);dialog.addEventListener('toggle',autonomyDescription);
var fields=['skills','software_tools','technical_skills','writing_research_skills','administrative_support_skills','domain_specific_skills','specialties'];
function label(item){return item.querySelector('input:not([type=checkbox])').value.trim();}
function sync(){var out=[];form.querySelectorAll('[data-chips] [data-collection-item]').forEach(function(item){if(item._experience&&!item.querySelector('[data-collection-remove]').checked&&label(item)){item._experience.label=label(item);out.push(item._experience);}});out.sort(function(a,b){return a.item_id.localeCompare(b.item_id);});hidden.value=JSON.stringify(out);}
function describe(item){var record=item._experience,summary=item.querySelector('[data-item-summary]'),button=item.querySelector('[data-experience-open]');button.textContent=record?'Edit experience details':'Add experience details';var parts=[];if(record){if(record.contexts.length)parts.push(record.contexts.map(function(c){return {study:'study or training',projects:'projects',professional:'professional use'}[c];}).join(', '));if(record.autonomy!=='unknown')parts.push({guided:'with guidance',independent:'independent routine tasks',complex:'independent complex tasks'}[record.autonomy]);if(record.months!==null)parts.push(record.months===0?'less than one month':'about '+record.months+' months');}summary.textContent=parts.length?parts.join(' · ')+' · self-reported':'';summary.hidden=!parts.length;}
function close(){dialog.close();if(opener)opener.focus();}
function attach(item,field){if(item._experienceReady)return;item._experienceReady=true;item._experience=records.find(function(r){return r.field===field&&r.label===label(item);})||null;
 var button=document.createElement('button');button.type='button';button.className='button-quiet';button.dataset.experienceOpen='';var summary=document.createElement('p');summary.dataset.itemSummary='';item.append(button,summary);describe(item);
 button.addEventListener('click',function(){if(!label(item)){item.querySelector('input').focus();return;}current=item;opener=button;var r=item._experience;dialog.querySelector('[data-item-label]').textContent=label(item);dialog.querySelectorAll('[data-item-context]').forEach(function(i){i.checked=!!r&&r.contexts.indexOf(i.value)!==-1;});dialog.querySelector('[data-item-autonomy]').value=r?r.autonomy:'unknown';autonomyDescription();dialog.querySelector('[data-item-months]').disabled=false;dialog.querySelector('[data-item-months]').value=r&&r.months!==null?r.months:'';dialog.querySelector('[data-item-error]').hidden=true;dialog.showModal();});
 item.addEventListener('input',function(e){if(e.target.type!=='checkbox'){sync();describe(item);}});
}
form.querySelectorAll('[data-chips]').forEach(function(group){var field=group.dataset.chips;if(fields.indexOf(field)===-1)return;
 group.querySelectorAll('[data-chip-items] [data-collection-item]').forEach(function(item){attach(item,field);});
 group.addEventListener('change',function(e){if(!e.target.matches('[data-collection-remove]'))return;var item=e.target.closest('[data-collection-item]');if(e.target.checked){var undo=document.createElement('button');undo.type='button';undo.className='button-quiet';undo.dataset.undoItem='';undo.textContent='Undo removal: '+label(item);group.appendChild(undo);undo.addEventListener('click',function(){e.target.checked=false;item.hidden=false;item.removeAttribute('aria-hidden');e.target.dispatchEvent(new Event('input',{bubbles:true}));sync();undo.remove();item.querySelector('input').focus();});}sync();});
 group.querySelector('[data-add-chip]').addEventListener('click',function(){setTimeout(function(){group.querySelectorAll('[data-chip-items] [data-collection-item]').forEach(function(item){attach(item,field);});},0);});
});
dialog.querySelector('[data-item-cancel]').addEventListener('click',close);
dialog.querySelector('[data-item-clear]').addEventListener('click',function(){current._experience=null;sync();describe(current);close();});
dialog.querySelector('[data-item-save]').addEventListener('click',function(){var input=dialog.querySelector('[data-item-months]');if(!input.checkValidity()){input.reportValidity();return;}var contexts=Array.from(dialog.querySelectorAll('[data-item-context]:checked')).map(function(i){return i.value;}).sort(),autonomy=dialog.querySelector('[data-item-autonomy]').value,months=input.value===''?null:Number(input.value);var bytes=new Uint8Array(16);crypto.getRandomValues(bytes);var id=current._experience?current._experience.item_id:Array.from(bytes).map(function(n){return n.toString(16).padStart(2,'0');}).join('');current._experience=(contexts.length||autonomy!=='unknown'||months!==null)?{item_id:id,field:current.closest('[data-chips]').dataset.chips,label:label(current),contexts:contexts,autonomy:autonomy,months:months,basis:'self_reported'}:null;sync();describe(current);close();});
dialog.addEventListener('keydown',function(e){if(e.key==='Enter'&&e.target.tagName!=='BUTTON')e.preventDefault();});
dialog.addEventListener('close',function(){dialog.querySelector('[data-item-months]').disabled=true;if(opener)opener.focus();});
sync();
})();
"""
