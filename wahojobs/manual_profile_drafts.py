"""Unconfirmed manual checkpoints in the existing private draft sidecar.

Only an authenticated account/principal/environment binding selects a row.
These values are never confirmation artifacts or authoritative profile revisions.
"""
import hashlib
import json
import secrets
from wahojobs import profile_correction_drafts as storage

TABLE = 'manual_profile_drafts'


class StaleManualDraft(ValueError):
    pass


def load(account_connection, owner):
    with storage.store_connection(account_connection) as connection:
        if connection is None or not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return None
        row = connection.execute('SELECT reference,payload_json,payload_sha256 FROM '+TABLE+
                                 ' WHERE owner_binding=?', (owner,)).fetchone()
        if row is None:
            return None
        value = row[1]
        if len(value.encode('utf-8')) > storage.MAX_PAYLOAD_BYTES or hashlib.sha256(value.encode('utf-8')).hexdigest() != row[2]:
            raise ValueError('manual_checkpoint_unavailable')
        payload = json.loads(value)
        if storage.encode(payload) != value:
            raise ValueError('manual_checkpoint_unavailable')
        return row[0], payload


def save(account_connection, owner, expected, payload):
    value = storage.encode(payload)
    with storage.store_connection(account_connection, write=True) as connection, connection:
        connection.execute('CREATE TABLE IF NOT EXISTS '+TABLE+' ('
            'owner_binding TEXT PRIMARY KEY, reference TEXT NOT NULL, '
            'payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)')
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT reference,payload_json FROM '+TABLE+' WHERE owner_binding=?', (owner,)).fetchone()
        # An exact retry after a lost response is safe; different stale content is not.
        if row and row[1] == value:
            return row[0]
        if (row[0] if row else '') != expected:
            raise StaleManualDraft('newer_manual_progress')
        reference = secrets.token_urlsafe(18)
        connection.execute('INSERT INTO '+TABLE+' VALUES (?,?,?,?) ON CONFLICT(owner_binding) '
            'DO UPDATE SET reference=excluded.reference,payload_json=excluded.payload_json,payload_sha256=excluded.payload_sha256',
            (owner, reference, value, hashlib.sha256(value.encode('utf-8')).hexdigest()))
        return reference


SCRIPT = r"""(function(){
'use strict';
var form=document.querySelector('form[data-manual-draft]');if(!form)return;
var status=document.getElementById('manual-draft-status'),busy=false,timer,dirty=false,generation=0;
function show(message,error){status.textContent=message;status.setAttribute('role',error?'alert':'status');}
function fields(){var data=new URLSearchParams(new FormData(form));data.delete('credentials_confirmed');return data;}
function material(){var data=fields();data.delete('manual_checkpoint');return data.toString();}
var observed=material();
function save(){
 if(busy)return Promise.resolve(false);busy=true;var submittedGeneration=generation;var data=fields();data.set('manual_action','save');
 return fetch(form.action,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body:data.toString()})
 .then(function(r){return r.json().then(function(result){if(!r.ok)throw new Error(result.error||'Progress could not be saved. Retry or return to your saved progress.');form.elements.manual_checkpoint.value=result.checkpoint;dirty=generation!==submittedGeneration;show(dirty?'Unsaved changes':'Draft saved. Nothing is confirmed yet.',false);if(dirty)timer=setTimeout(save,700);return true;});})
 .catch(function(e){show(e.message||'Progress could not be saved. Keep this page open and retry.',true);return false;})
 .finally(function(){busy=false;});
}
function changed(){var current=material();if(current===observed)return;observed=current;dirty=true;generation++;clearTimeout(timer);show('Unsaved changes',false);timer=setTimeout(save,700);}
['input','change','click'].forEach(function(kind){form.addEventListener(kind,function(){Promise.resolve().then(changed);});});
var saveButton=document.getElementById('save-manual-draft');if(saveButton)saveButton.addEventListener('click',function(){clearTimeout(timer);save();});
form.addEventListener('submit',function(e){if(busy){e.preventDefault();show('Saving your draft. Please try Continue again in a moment.',false);return;}clearTimeout(timer);dirty=false;});
window.addEventListener('beforeunload',function(e){if(dirty){e.preventDefault();e.returnValue='';}});
})();"""


def controls(reference):
    from html import escape
    return ("<input type='hidden' name='manual_checkpoint' value='"+escape(reference, quote=True)+"'>"
            "<p id='manual-draft-status' role='status' aria-live='polite'>Your progress is an unconfirmed draft. "
            "Changes are saved while you type.</p>"
            "<button type='button' id='save-manual-draft'>Save draft</button>"
            "<p><a href='/find-matches'>Return to saved progress</a></p>")
