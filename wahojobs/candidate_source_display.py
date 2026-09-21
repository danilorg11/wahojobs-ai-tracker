"""Small, source-only formatting helpers; never used for matching decisions."""
from html import escape, unescape
import re
from urllib.parse import urlsplit


def plain(text):
    """Remove presentation delimiters without changing qualification wording."""
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
    return re.sub(r'[*`_]', '', text).strip()


def _inline(text):
    # Escape first. Images never fetch. Only explicit HTTP(S) links are clickable.
    text = escape(text, quote=True)
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'Image: \1', text)
    def link(match):
        label, destination = match.groups()
        url = unescape(destination)
        try:
            parsed = urlsplit(url)
            safe = (parsed.scheme in ('http', 'https') and parsed.hostname
                    and not parsed.username and not parsed.password
                    and not any(ord(char) < 33 for char in url))
        except ValueError:
            safe = False
        return (f"<a href='{escape(url, quote=True)}' target='_blank' rel='noopener noreferrer nofollow'>{label}</a>"
                if safe else label)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link, text)
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', text)
    return text


def markdown(text, *, heading_level=3):
    """Render the captured heading/paragraph/list subset without trusting HTML.

    Unsupported Markdown stays escaped text. No scripts, raw HTML, embedded
    resources or extension/plugin execution is supported.
    """
    output, paragraph, lists = [], [], []
    def flush():
        if paragraph:
            output.append('<p>' + _inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
    def close_lists(minimum=-1):
        while lists and lists[-1][0] >= minimum:
            _, kind = lists.pop()
            output.append('</li></' + kind + '>')
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        heading = re.fullmatch(r'#{1,6}\s+(.+?)\s*#*', stripped)
        bold_heading = re.fullmatch(r'\*\*([^*]{1,110})\*\*:?\s*', stripped)
        item = re.match(r'^(\s*)(?:([-+*])\s+|(\d+)[.)]\s+)(.*)', line)
        if heading or bold_heading:
            flush(); close_lists()
            label = (heading or bold_heading).group(1).rstrip(':')
            output.append(f'<h{heading_level}>' + _inline(label) + f'</h{heading_level}>')
        elif item:
            flush()
            indent, kind = len(item[1].expandtabs(4)), 'ol' if item[3] else 'ul'
            while lists and (lists[-1][0] > indent or
                             (lists[-1][0] == indent and lists[-1][1] != kind)):
                close_lists(lists[-1][0])
            if lists and lists[-1][0] == indent:
                output.append('</li><li>')
            else:
                output.append('<' + kind + '><li>'); lists.append((indent, kind))
            output.append(_inline(item[4]))
        else:
            # Indented wrapped lines continue a list item, not a new requirement.
            if lists and line[:1].isspace():
                output.append(' ' + _inline(stripped))
            else:
                close_lists(); paragraph.append(stripped)
    flush(); close_lists()
    return ''.join(output)


from wahojobs.profiles.preference_model import ISO_4217_CURRENCIES
_CURRENCY_CODES = '|'.join(sorted(ISO_4217_CURRENCIES))
_CURRENCY = re.compile(r'\b(?:' + _CURRENCY_CODES + r')\b', re.I)

_RATE = re.compile(
    r'(?<![\w.])(?:(?:up to|from|starting at|at least)\s+)?'
    rf'(?:(?:{_CURRENCY_CODES})\s*[$€£]?|[\$€£])?\d+(?:[,.]\d+)*\+?'
    rf'(?:\s*(?:[-–—]\s*to\s*[-–—]|to\b|[-–—])\s*(?:(?:{_CURRENCY_CODES})\s*[$€£]?|[\$€£])?\d+(?:[,.]\d+)*\+?)?'
    rf'\s*(?:(?:{_CURRENCY_CODES})\s*)?(?:/\s*|per\s+)'
    rf'(?:accepted\s+)?(?:hour|hr|month|year|project|task)s?\b(?:\s+(?:{_CURRENCY_CODES})\b)?', re.I)


def pay_facts(metadata, text):
    """Quote explicit pay phrases; do not turn an upper limit into exact pay.

    Only accepted pay fields and source wording are considered. In particular,
    canonical enrichment notes are not a compensation fact for this variant.
    """
    from wahojobs.crawler.provider_details import DETAIL_KEY
    detail = metadata.get(DETAIL_KEY)
    detail = detail if isinstance(detail, dict) else {}
    record = detail.get('record')
    record = record if isinstance(record, dict) else {}
    phrases = []
    # The accepted listing pay field retains the literal currency symbol/unit.
    for value in (metadata.get('pay'), text, record.get('shortDescription')):
        if isinstance(value, str):
            phrases.extend(m.group().strip() for m in _RATE.finditer(plain(value)))
    phrases = list(dict.fromkeys(phrases))
    # A captured detail may contain a typed hourly range without a pay teaser.
    # Preserve those bounds without inventing a dollar symbol or denomination.
    if not phrases and record.get('salaryType') == 'HOURLY':
        lo, hi = record.get('lowerBoundHourlyRate'), record.get('upperBoundHourlyRate')
        valid = lambda n: type(n) in (int, float) and n >= 0
        if valid(lo) and valid(hi) and lo <= hi:
            amount = f'{lo:g}' if lo == hi else f'{lo:g}–{hi:g}'
            phrases.append(f'{amount} per hour (currency not specified)')
        elif valid(lo) and hi is None:
            phrases.append(f'From {lo:g} per hour (currency not specified)')
        elif lo is None and valid(hi):
            phrases.append(f'Up to {hi:g} per hour (currency not specified)')
    output_pay = bool(re.search(r'\bcompensation is output-based\b', text, re.I))
    task_pay = bool(re.search(r'\b(?:experts are|you are|you will be) paid per '
                              r'(?:accepted task|task that meets)\b', text, re.I))
    # micro1's accepted detail has a numeric hourly range, but no currency.
    # Preserve this explicitly as a separate term, not an inferred task rate.
    hourly = record.get('ideal_hourly_rate')
    if isinstance(hourly, dict):
        lo, hi = hourly.get('min'), hourly.get('max')
        if all(type(n) in (int, float) and n >= 0 for n in (lo, hi)) and lo <= hi:
            amount = f'{lo:g}' if lo == hi else f'{lo:g}–{hi:g}'
            phrases.append(f'{amount} per hour (currency not specified)')
    label = ('Per accepted task' if task_pay else 'Output-based pay' if output_pay
             else (phrases[0] if phrases else ''))
    notes = []
    if (task_pay or output_pay) and phrases:
        notes.append(('Task-based pay' if task_pay else 'Output-based pay')
                     + ' and rates are both listed; confirm the payment terms.')
    elif len(phrases) > 1 and not _compatible_pay_wording(phrases):
        notes.append('The source uses more than one pay description; confirm the applicable rate.')
    currency_note = ''
    # A captured structured salary denomination belongs to this listing's pay.
    # A denomination on another quoted rate/unit does not denominate this rate.
    record_currencies = {record[key].upper() for key in ('salaryCurrency', 'currency', 'currencyCode')
                         if isinstance(record.get(key), str) and record[key].upper() in ISO_4217_CURRENCIES}
    record_currency = next(iter(record_currencies)) if len(record_currencies) == 1 else None
    unknown = lambda phrase: bool(re.search(r'\d', phrase) and not _CURRENCY.search(phrase) and not re.search(r'[€£]', phrase))
    if record_currency:
        # Retain literal advertised amounts and symbols; add only source-backed denomination.
        def denominated(phrase):
            if 'currency not specified' in phrase:
                return phrase.replace('(currency not specified)', record_currency)
            if unknown(phrase):
                return phrase + ' ' + record_currency
            return phrase
        label = denominated(label)
        phrases = [denominated(phrase) for phrase in phrases]
    elif any(unknown(phrase) for phrase in phrases):
        currency_note = 'Currency not specified in the listing.'
    return {'label': label, 'wording': phrases, 'notes': notes, 'currency_note': currency_note}



def _compatible_pay_wording(phrases):
    """A range and its matching upper-limit teaser need no conflict warning.

    Both quotations are still retained. Different currencies, units, numeric
    bounds or ambiguous '+' qualifiers cannot be reconciled here.
    """
    first = phrases[0].lower()
    numbers = re.findall(r'\d+(?:[,.]\d+)*', first)
    if len(numbers) != 2 or '+' in first:
        return False
    def currency(value):
        return re.findall(r'\b(?:usd|eur|gbp|cad|aud)\b|[$€£]', value)
    def unit(value):
        return re.sub(r'\bhrs?\b', 'hour', value).split('/')[-1].split('per ')[-1].strip()
    for phrase in phrases[1:]:
        phrase = phrase.lower()
        if '+' in phrase or set(currency(first)) != set(currency(phrase)) or unit(first) != unit(phrase):
            return False
        other = re.findall(r'\d+(?:[,.]\d+)*', phrase)
        if other != numbers and not (phrase.startswith('up to ') and other == numbers[-1:]):
            return False
    return True


DISPLAY_CSS = """
.decision-relevance {border-left:3px solid #337257;padding:0 0 0 14px;margin:18px 0;line-height:1.55}
.decision-relevance h4,.decision-relevance h2 {font-size:.95rem;margin:0 0 6px}
.candidate-detail .brand {flex-shrink:0;white-space:nowrap}
@media(max-width:500px){.candidate-detail .site-header{flex-wrap:wrap;gap:12px}.candidate-detail .site-header nav{margin-left:auto}}
.decision-relevance ul {padding-left:18px;margin:0}
.decision-relevance .candidate-note {margin:8px 0 0;font-size:.82rem}
.decision-points {list-style:none!important;padding:0!important;display:grid;gap:10px;margin:12px 0}
.decision-point {padding:12px 14px!important;background:#f6f8f7;border-radius:8px;border-left:3px solid #8b9b93;line-height:1.5}
.decision-point > strong {font-size:.86rem;color:#304a3e}
.decision-modality {display:inline-block;font-size:.75rem;padding:1px 5px;border:1px solid #c6cec9;border-radius:4px;margin-left:5px}
.match-card-actions .js-card-controls button {background:#fff;color:#175540;border:1px solid #b8cfc3}
.decision-point > p {margin:5px 0!important;font-size:.92rem}
.decision-conflict {border-color:#ad5145;background:#fcf2ef}
.decision-conflict > strong {color:#8c3025}
.decision-supported,.decision-waived {border-color:#49866a}
.decision-source summary {font-size:.85rem;padding:10px 0;min-height:44px;cursor:pointer}
.decision-source>blockquote {margin:4px 0 10px;padding:8px 12px;border-left:2px solid #b8c5bd;font-style:normal}
.decision-assessment {scroll-margin-top:20px}
.decision-assessment h2 {font-size:1.15rem;margin:16px 0}
.decision-placement {font-size:.92rem;line-height:1.5}
.candidate-profile-next {font-size:.88rem;padding:10px 14px;margin:14px 0;background:#f0f5f2;border-radius:8px;line-height:1.5}
.candidate-profile-next p {margin:5px 0}
.candidate-profile-update {display:inline-block;min-height:32px;font-weight:650}
.conditional-matches {margin-top:30px;border-top:1px solid #d9e2dc;padding-top:20px}
.conditional-matches > h2 {font-size:1.4rem;margin:0 0 8px}
.conditional-matches > p {max-width:70ch;line-height:1.6;color:#52645b;margin:0 0 18px}
.conditional-card {border-color:#d7c9ad}
.conditional-card .match-rank-label {color:#7b5d2c}
.workflow-assessment-note {max-width:760px;line-height:1.6;color:#52645b;margin:12px 0 20px}
.match-card,.candidate-detail,.candidate-detail * {min-width:0;overflow-wrap:anywhere}
.match-meta-item strong {display:block}
.candidate-detail :focus-visible,.match-card :focus-visible {outline:3px solid #146149;outline-offset:3px}
.candidate-detail .decision-assessment {margin:24px 0}
@media(min-width:850px){.candidate-detail .hero:has(.workflow-card){display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:24px;align-items:start}.candidate-detail .workflow-card{margin-top:0}}
@media(max-width:500px){.match-card-actions{width:100%}.match-card-actions .button{width:100%;text-align:center}.candidate-detail .hero-actions{display:flex;flex-wrap:wrap}.decision-point{padding:10px!important}}
.source-description {overflow-wrap:anywhere;line-height:1.7}
.source-description h3,.source-description h4 {margin:1.5em 0 .55em;line-height:1.35}
.source-description p {margin:.65em 0}
.source-description ul,.source-description ol {padding-left:1.5em;margin:.7em 0}
.source-description li {padding-left:.15em;margin:.35em 0}
.source-description li p {margin:0}
.candidate-conditions {border:1px solid #d6e3dc;border-radius:12px;margin:16px 0}
.candidate-conditions summary {padding:12px 15px;cursor:pointer;font-weight:650}
.candidate-conditions summary:focus-visible {outline:3px solid #146149;outline-offset:3px}
.candidate-conditions .source-description {padding:0 16px 16px}
.candidate-conditions .source-description h4 {font-size:.95rem}
.candidate-note {color:#52645b;font-size:.9rem}
.candidate-caveats {padding-left:1.2em;color:#52645b;font-size:.92rem;line-height:1.55}
.candidate-caveats li {margin:.35em 0}
.candidate-kind {color:#22583f;font-size:.8rem;font-weight:700}
.candidate-overview {line-height:1.6;margin:14px 0}
.candidate-status {padding:14px 18px;border-left:3px solid #aa7d36;background:#faf5ea}
.candidate-detail .hero {display:block}
.candidate-detail .workflow-card {margin-top:20px;max-width:none}
.candidate-detail .fact-grid {grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.candidate-detail main {max-width:980px}
.candidate-detail .job-description {max-width:800px}
.candidate-detail .verification-footer {font-size:.84rem}
.matches-hero {margin:22px 0 10px}
.matches-hero h1 {font-size:2.5rem;margin:0 0 8px}
.matches-summary {font-size:1.05rem;margin:0}
.matches-profile-context {background:none;border-radius:0;padding:0;margin:0 0 18px;gap:12px}
.match-card {padding:22px;gap:20px;scroll-margin-top:20px}
.match-rank-label {margin:0 0 6px}
.match-card h3 {margin:0 0 6px;font-size:1.4rem}
.match-company {margin:0 0 12px}
.match-meta {margin:0 0 12px;gap:6px}
.match-meta-item {padding:7px 9px;min-width:120px}
.card-evidence .candidate-overview {line-height:1.5}
.candidate-caveats {margin:10px 0;line-height:1.45}
.candidate-conditions {margin:10px 0}
.candidate-conditions summary {min-height:44px;padding:10px 12px}
.candidate-detail .hero {padding:24px;margin-bottom:20px}
.candidate-detail .hero h1 {font-size:2.2rem;line-height:1.2;margin:0 0 8px}
.candidate-detail .fact-grid {margin:18px 0;gap:8px}
.candidate-detail .fact {padding:10px 12px}
.candidate-detail .candidate-checks {margin:14px 0}
.candidate-detail .candidate-checks h2 {font-size:1rem;margin:0 0 8px}
.candidate-detail .candidate-checks .candidate-note {margin:8px 0}
.candidate-detail .content-section {padding:20px 0}
.candidate-detail .company-line {margin:0 0 10px}
.candidate-detail .source-description h3 {scroll-margin-top:20px}
.decision-fit {font-size:1rem;line-height:1.55;margin:12px 0;color:#244d3b;max-width:70ch}
.decision-warning {border-left:3px solid #b88c46;background:#fff9ef;padding:9px 12px;margin:12px 0;font-size:.9rem;max-width:72ch}
.decision-warning p {margin:5px 0;line-height:1.5}
.decision-warning > strong {font-size:.85rem;color:#76551f}
.decision-warning-conflict {border-color:#ad5145;background:#fff3ef}
.decision-warning-conflict > strong {color:#8c3025}
.candidate-checks .application-guidance {line-height:1.65;max-width:72ch;margin:10px 0}
.candidate-profile-next {background:none;padding:0;margin:12px 0;font-size:.86rem;color:#52645b}
.candidate-profile-next a {display:inline-flex;align-items:center;min-height:44px}
.employer-description,.candidate-support {border:1px solid #d9e0dc;border-radius:10px;background:white;margin:14px 0}
.employer-description > summary,.candidate-support > summary {padding:14px 16px;min-height:48px;font-weight:650;cursor:pointer}
.employer-description > .source-description {padding:0 18px 18px}
.candidate-support > p,.candidate-support > blockquote {margin:8px 18px 14px}
.candidate-support > blockquote {border-left:2px solid #b8c5bd;padding:8px 12px}
.employer-description > summary:focus-visible,.candidate-support > summary:focus-visible {outline:3px solid #2563eb;outline-offset:3px}
.matches-profile-context {justify-content:flex-start;gap:8px}
.matches-profile-context span {font-size:.84rem;color:#657168}
@media(max-width:680px){
  .matches-hero {margin:16px 0 10px}.matches-hero h1 {font-size:2rem}
  .matches-profile-context {flex-direction:row;align-items:center;margin-bottom:14px}
  .match-card {padding:16px;gap:10px}.match-card h3 {font-size:1.25rem}
  .match-meta-item {min-width:0}.match-card .candidate-caveats {font-size:.9rem}
  .candidate-detail .hero {padding:18px}.candidate-detail .hero h1 {font-size:1.7rem}
  .candidate-detail .fact-grid {margin:14px 0}
}
@media(max-width:600px){.candidate-detail .fact-grid {grid-template-columns:1fr 1fr}
.candidate-detail .fact {min-width:0}.candidate-detail dd {overflow-wrap:anywhere}}
@media(max-width:380px){.candidate-detail .fact-grid {grid-template-columns:1fr}}
"""
