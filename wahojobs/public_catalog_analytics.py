"""Existing GA4 destination, limited to anonymous public catalog documents.

Gatekeeper owns regional consent and persistence. No account, candidate action,
query, source text or profile data is used here. No GA property configuration is
changed. The external Google tag is not loaded until CMP resolution permits it.
"""

MEASUREMENT_ID = 'G-QFMW1WX907'
FRAME_PATH = '/jobs/_analytics'
FRAME_CSP = ("default-src 'none'; script-src 'unsafe-inline' https://www.googletagmanager.com; "
             "connect-src https://www.google-analytics.com https://region1.google-analytics.com; "
             "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")

# Explicit hosts, not a general https: permission. The CMP renders its own UI.
CSP = (
    "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com "
    "https://the.gatekeeperconsent.com https://privacy.gatekeeperconsent.com; "
    "connect-src 'self' https://www.google-analytics.com https://region1.google-analytics.com "
    "https://privacy.gatekeeperconsent.com https://the.gatekeeperconsent.com "
    "https://gvl.gatekeeperconsent.com https://g.ezoic.net; frame-src 'self'; "
)

SCRIPT = r"""
(function () {
  'use strict';
  var id = 'G-QFMW1WX907';
  var origin = 'https://www.wahojobs.com';
  var path = window.location.pathname;
  // Never initialize this integration on login, callback, account or API routes.
  if (window.location.origin !== origin ||
      (window.parent && window.parent !== window) ||
      !/^\/jobs(?:\/opportunity-[1-9][0-9]*)?$/.test(path) ||
      window.__wahojobsPublicAnalytics) return;
  window.__wahojobsPublicAnalytics = true;
  var allowed = false, loaded = false, viewed = false, listening = false, frame;
  var disable = 'ga-disable-' + id;
  window[disable] = true;
  window.dataLayer = window.dataLayer || [];
  function queue() {
    window.dataLayer.push(arguments);
    if (frame) {
      try {
        if (frame.contentWindow.__wahojobsQueue) {
          frame.contentWindow.__wahojobsQueue(JSON.stringify(Array.prototype.slice.call(arguments)));
        }
      } catch (_) { /* An unavailable/blocked frame cannot collect. */ }
    }
  }
  function consent(value) {
    return {analytics_storage: value, ad_storage: 'denied',
      ad_user_data: 'denied', ad_personalization: 'denied'};
  }
  queue('consent', 'default', consent('denied'));
  queue('set', 'url_passthrough', false);
  queue('set', 'ads_data_redaction', true);
  // The existing CMP can call gtag; analytics still follows its resolved TCF
  // decision, and this catalog integration does not enable advertising.
  window.gtag = function (command, action) {
    if (command === 'consent' && (action === 'default' || action === 'update')) {
      queue('consent', action, consent(allowed ? 'granted' : 'denied'));
    }
  };
  var params = {page_location: origin + path,
    page_title: path === '/jobs' ? 'AI Training Jobs | Wahojobs' : 'Job opportunity | Wahojobs',
    page_referrer: '', send_to: id};
  function apply(value) {
    if (allowed === value) return;
    allowed = value;
    window[disable] = !allowed;
    if (!allowed && frame) {
      try { frame.contentWindow[disable] = true; } catch (_) { /* Still remove below. */ }
    }
    queue('consent', 'update', consent(allowed ? 'granted' : 'denied'));
    if (!allowed) {
      if (frame) {
        // Stop even automatic engagement/consent traffic after withdrawal.
        frame.remove();
        frame = null;
      }
      return;
    }
    if (!loaded) {
      loaded = true;
      queue('js', new Date());
      queue('config', id, {send_page_view: false,
        page_location: params.page_location, page_title: params.page_title,
        page_referrer: '', allow_google_signals: false,
        allow_ad_personalization_signals: false, cookie_flags: 'SameSite=Lax;Secure'});
    }
    if (!viewed) {
      viewed = true;
      queue('event', 'page_view', params);
      // The existing property enables automatic search/form/history tracking.
      // A URL override does not stop it reading q from the current document.
      // Its blank same-origin document has no search, forms, links, user text
      // or SPA history. Cookies keep the existing first-party GA scope. This
      // is collector isolation, not a security boundary against trusted GA.
      frame = document.createElement('iframe');
      frame.id = 'wahojobs-public-ga';
      frame.hidden = true;
      frame.title = 'Anonymous page measurement';
      frame.setAttribute('aria-hidden', 'true');
      frame.referrerPolicy = 'no-referrer';
      var commands = window.dataLayer.map(function (entry) {
        return Array.prototype.slice.call(entry);
      });
      // A dedicated, empty first-party document preserves cookie scope without
      // loading catalog content or candidate controls into the collector.
      frame.onload = function () {
        if (!frame || !allowed) return;
        frame.onload = null;
        frame.contentWindow.postMessage({type: 'wahojobs-public-measurement',
          commands: JSON.stringify(commands)}, origin);
      };
      frame.src = '/jobs/_analytics';
      document.head.appendChild(frame);
    }
  }
  function resolved() {
    if (typeof window.__tcfapi === 'function') {
      if (listening) return;
      listening = true;
      window.__tcfapi('addEventListener', 2, function (data, success) {
        if (!success || !data || data.cmpStatus !== 'loaded') { apply(false); return; }
        if (data.gdprApplies === false) { apply(true); return; }
        if (data.gdprApplies !== true ||
            (data.eventStatus !== 'tcloaded' && data.eventStatus !== 'useractioncomplete')) {
          apply(false); return;
        }
        // Uses Gatekeeper's purpose-1 / Google vendor-755 check. Explicit
        // denial wins; missing purpose/vendor structures remain unresolved.
        apply(!!(data.purpose && data.purpose.consents &&
          data.purpose.consents[1] === true && data.vendor && data.vendor.consents &&
          data.vendor.consents[755] !== false));
      });
    } else {
      // Gatekeeper's no-TCF regional policy is usable only after its region
      // request completes. Its early ezConsentEvent alone is not permission.
      apply(!!(window.ezCMPQueue && window.ezCMPQueue.gotResponse === true &&
        window.ezTcfConsent && window.ezTcfConsent.loaded === true &&
        window.ezTcfConsent.store_info === true));
    }
  }
  var cmp = document.createElement('script');
  cmp.src = 'https://the.gatekeeperconsent.com/cmp.min.js';
  cmp.setAttribute('data-cfasync', 'false');
  cmp.referrerPolicy = 'no-referrer';
  cmp.onload = function () {
    if (window.ezCMPQueue && typeof window.ezCMPQueue.push === 'function') {
      window.ezCMPQueue.push(resolved);
    }
  };
  // CMP/network failure leaves Google disabled; no timeout grants consent.
  document.head.appendChild(cmp);
}());
"""

HEAD = '<script id="wahojobs-public-analytics">' + SCRIPT + '</script>'

# A top-level visit cannot initialize Google. Only the same-origin parent that
# has already resolved consent may start this empty document. No query accepted.
FRAME_DOCUMENT = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="robots" content="noindex,follow">
<meta name="referrer" content="no-referrer"><title>Anonymous page measurement</title>
<script>
(function () {
  if (window.parent === window || window.location.origin !== 'https://www.wahojobs.com') return;
  function initialize(event) {
    if (event.source !== window.parent || event.origin !== 'https://www.wahojobs.com' ||
        !event.data || event.data.type !== 'wahojobs-public-measurement') return;
    window.removeEventListener('message', initialize);
    if (window.parent['ga-disable-G-QFMW1WX907'] !== false) return;
    var parentPath = window.parent.location.pathname;
    if (!/^\/jobs(?:\/opportunity-[1-9][0-9]*)?$/.test(parentPath)) return;
    var expectedLocation = 'https://www.wahojobs.com' + parentPath;
    var expectedTitle = parentPath === '/jobs' ? 'AI Training Jobs | Wahojobs' : 'Job opportunity | Wahojobs';
    // The property's automatic collectors are not controlled by send_page_view.
    // Limit this document's network transport to one clean manual pageview.
    // This gate is local to the empty iframe, never the product's fetch/XHR.
    var sent = false;
    function permitted(url, body) {
      if (sent || window.parent['ga-disable-G-QFMW1WX907'] !== false) return false;
      if (body != null && typeof body !== 'string') return false;
      try {
        var destination = new URL(url);
        if (!/^https:\/\/(?:www|region1)\.google-analytics\.com$/.test(destination.origin) ||
            destination.username || destination.password || destination.hash ||
            destination.pathname !== '/g/collect') return false;
        var values = new URLSearchParams(destination.search);
        var repeated = false, seen = Object.create(null);
        values.forEach(function (value, key) {
          if (seen[key]) repeated = true;
          seen[key] = true;
        });
        new URLSearchParams(body || '').forEach(function (value, key) {
          if (seen[key]) repeated = true;
          seen[key] = true;
          values.append(key, value);
        });
        if (repeated) return false;
        if (values.get('tid') !== 'G-QFMW1WX907' || values.get('en') !== 'page_view' ||
            values.get('dl') !== expectedLocation ||
            (values.get('dr') || '') !== '' ||
            values.get('dt') !== expectedTitle) return false;
        var clean = true;
        values.forEach(function (value, key) {
          if (/^(?:ep\.|epn\.|up\.|upn\.|user_|uid$|ud\.|em$|ph$|pn$)/.test(key) || /[\r\n]/.test(value)) clean = false;
        });
        return clean;
      } catch (_) { return false; }
    }
    var beacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, body) {
      if (!permitted(url, body)) return false;
      var accepted = beacon(url, body);
      if (accepted) sent = true;
      return accepted;
    };
    var fetchRequest = window.fetch.bind(window);
    window.fetch = function (url, options) {
      if (!permitted(url, options && options.body)) return Promise.reject(new TypeError('Measurement request blocked'));
      sent = true;
      return fetchRequest(url, options);
    };
    var open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__wahojobsUrl = url;
      return open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
      if (!permitted(this.__wahojobsUrl, body)) { this.abort(); return; }
      sent = true;
      return send.apply(this, arguments);
    };
    window.dataLayer = [];
    function gtag() { window.dataLayer.push(arguments); }
    window.__wahojobsQueue = function (value) {
      var args = JSON.parse(value);
      if (args[0] === 'js') args[1] = new Date(args[1]);
      gtag.apply(null, args);
    };
    JSON.parse(event.data.commands).forEach(function (args) {
      window.__wahojobsQueue(JSON.stringify(args));
    });
    var tag = document.createElement('script');
    tag.async = true;
    tag.referrerPolicy = 'no-referrer';
    tag.src = 'https://www.googletagmanager.com/gtag/js?id=G-QFMW1WX907';
    document.head.appendChild(tag);
  }
  window.addEventListener('message', initialize);
}());
</script></head><body></body></html>"""
