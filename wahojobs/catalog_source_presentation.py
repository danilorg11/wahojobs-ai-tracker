"""Bounded source-owned presentation, prepared once per public generation.

Retained documents stay immutable. No fetching, semantic enrichment, account
data or lifecycle authority is introduced by these display projections.
"""
from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

PRESENTATION_VERSION = 4
_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
_OMIT = {'script', 'style', 'nav', 'footer', 'form', 'button', 'input', 'select', 'textarea', 'svg', 'iframe'}


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def descendants(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.descendants()


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        if len(text) > 2_000_000:
            raise ValueError('source_document_too_large')
        self.root = Node()
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            if len(self.stack) > 128:
                raise ValueError('source_document_too_deep')
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _markdown(node, depth=0):
    if isinstance(node, str):
        return re.sub(r'\s+', ' ', node)
    if node.tag in _OMIT:
        return ''
    if {'field__label', 'visually-hidden'} <= set(node.attrs.get('class', '').split()):
        return ''  # Drupal's decorative image-field label is not article copy.
    inner = ''.join(_markdown(child, depth + (node.tag in ('ul', 'ol'))) for child in node.children)
    if node.tag == 'br':
        return '\n'
    if re.fullmatch('h[1-6]', node.tag):
        return '\n\n## ' + inner.strip() + '\n\n'
    if node.tag == 'li':
        return '\n' + '  ' * max(0, depth - 1) + '- ' + inner.strip() + '\n'
    if node.tag in ('p', 'div', 'section', 'ul', 'ol', 'blockquote'):
        return '\n\n' + inner.strip() + '\n\n'
    if node.tag in ('strong', 'b'):
        return '**' + inner.strip() + '**' if inner.strip() else ''
    if node.tag in ('em', 'i'):
        return '*' + inner.strip() + '*' if inner.strip() else ''
    if node.tag == 'a':
        destination = node.attrs.get('href', '')
        try:
            parsed = urlsplit(destination)
            safe = (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
                    and not parsed.password and not re.search(r'[\s()]', destination))
        except ValueError:
            safe = False
        if safe:
            return '[' + inner.strip() + '](' + destination + ')'
        if destination.startswith('mailto:'):
            address = destination[7:].split('?')[0]
            return inner if address in inner else inner + ' (' + address + ')'
    return inner


def _text(node):
    return re.sub(r'\s+', ' ', re.sub(r'[*#]', '', _markdown(node))).strip()


def bound_metadata(job):
    if (not job.get('external_id') or job.get('rich_provider') != job.get('company_slug')
            or job.get('rich_external_id') != job.get('external_id')
            or job.get('rich_source_url') != job.get('listing_url')):
        return {}
    try:
        value = json.loads(job.get('rich_metadata_json') or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def surge_role_fields(job, metadata=None):
    """The same page contains other roles: bind its popup by slug AND title."""
    if job.get('company_slug') != 'surge':
        return {}
    metadata = bound_metadata(job) if metadata is None else metadata
    document = metadata.get('detail_page_html')
    if not isinstance(document, str):
        return {}
    path = urlsplit(job.get('listing_url') or '').path.rstrip('/')
    slug = path.rsplit('/', 1)[-1]
    if not path.startswith('/workforce/') or job.get('external_id') != 'surge::workforce::' + slug:
        return {}
    try:
        matches = [n for n in Document(document).root.descendants()
                   if n.attrs.get('data-slug') == slug and 'workforce-popup' in n.attrs.get('class', '').split()]
    except (ValueError, TypeError):
        return {}  # Optional retained header cannot fail readiness for all roles.
    if len(matches) != 1:
        return {}
    fields = {}
    for node in matches[0].descendants():
        key = node.attrs.get('data-job')
        if key:
            value = _text(node)
            if key in fields and fields[key] != value:
                return {}
            fields[key] = value
    if fields.get('title') != job.get('source_title'):
        return {}
    return fields


def dataforce_description(job):
    """Select the exact project article, never a footer's similarly named field."""
    if (job.get('company_slug') != 'dataforce'
            or job.get('rich_provider') != job.get('company_slug')
            or job.get('rich_external_id') != job.get('external_id')
            or job.get('rich_source_url') != job.get('listing_url')):
        return None
    path = urlsplit(job.get('listing_url') or '').path.rstrip('/')
    articles = [n for n in Document(job.get('rich_body') or '').root.descendants()
                if n.tag == 'article' and 'node--type-project-page' in n.attrs.get('class', '').split()
                and urlsplit(n.attrs.get('about', '')).path.rstrip('/') == path]
    if len(articles) != 1:
        raise ValueError('dataforce_project_body_not_bound')
    bodies = [n for n in articles[0].descendants() if 'field--name-body' in n.attrs.get('class', '').split()]
    if len(bodies) != 1:
        raise ValueError('dataforce_project_body_not_unique')
    return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', _markdown(bodies[0])).strip()


def contribution_context(job, text, pay, surge_fields=None):
    """A small evidence-bound activity label, not a taxonomy or duty inference."""
    from wahojobs.candidate_source_display import plain
    title = job.get('source_title') or ''
    # Full paragraphs stay below. Only a source quotation becomes the preview.
    blocks, company_section = [], False
    for paragraph in text.split('\n\n'):
        value = plain(paragraph).strip('#: -')
        if not value:
            continue
        if len(value) < 110 and re.match(r'about\b', value, re.I) and re.search(
                r'\b(?:jobs?|roles?|projects?|opportunit(?:y|ies)|talent (?:network|pool))\b', value, re.I):
            company_section = False
            continue
        if (len(value) < 110 and re.match(r'about\b|who we are\b|our (?:company|mission)\b', value, re.I)
                and not re.search(r'\b(?:job|role|project|opportunity)\b', value, re.I)):
            company_section = True
            continue
        if len(value) < 110 and re.match(r'(?:the )?role\b|what (?:you|this|does|we)|(?:key |primary |main )?responsibilit|requirements?\b|'
                                       r'(?:task|project|job) (?:overview|details|description)|position\b|overview\b|summary\b|description\b|'
                                       r'about (?:the )?(?:job|role|project|opportunity)\b|your (?:role|responsibilit)', value, re.I):
            company_section = False
            continue
        if company_section or re.match(r'Based in [^\n]{0,80},? [^\n]{0,80}\b(?:leading|global)\b|'
                                       r'Our mission\b|About (?:us|the company)\b', value, re.I):
            continue
        blocks.append(value)
    ai_action = re.compile(r'\b(?:evaluat\w*|train\w*|review\w*|refin\w*|annotat\w*|improv\w*|build\w*|fine[- ]tun\w*|analy[sz]\w*)\b'
        r'.{0,160}\b(?:AI|artificial intelligence|machine learning|language models?|LLMs?|model-generated)\b|'
        r'\b(?:AI|artificial intelligence|machine learning|language models?|LLMs?|model-generated)\b'
        r'.{0,100}\b(?:evaluat\w*|train\w*|review\w*|refin\w*|annotat\w*|fine[- ]tun\w*)\b', re.I)
    # Do not interpret a company's mission or generic boilerplate as role duties.
    action = next((p for p in blocks if ai_action.search(p) and not re.match(
        r'about (?:us|the company)|our mission|[\w ]{0,25} is (?:a|the) (?:leading|global)', p, re.I)), '')
    fields = surge_fields or {}
    if fields.get('might-do') and ai_action.search(fields['might-do']):
        action = fields['might-do']
    contribution = bool(re.search(r'\b(?:data contributor|(?:speech|voice|audio|image|video|data) collection)\b', title, re.I)
                        or re.search(r"\b(?:we're|we are) building a .{0,60}dataset\b", text, re.I))
    if contribution:
        label = 'Paid data contribution' if pay else 'Data contribution'
        quote = next((p for p in blocks if re.search(r'\bdataset\b|collecte de données|collect.{0,30}(?:data|recordings)', p, re.I)
                      and len(p) > 70), '')
    elif re.search(r'\bsocial media evaluator\b', title, re.I) and (social := next((
            p for p in blocks if re.search(r'\b(?:review|evaluate)\b.{0,70}\bposts\b.{0,40}\bsocial media\b', p, re.I)), '')):
        label = 'Social media evaluation'
        quote = next((sentence.strip(' -') for sentence in re.split(r'(?<=[.!?])\s+|\n', social)
                      if re.search(r'\b(?:review|evaluate)\b.{0,70}\bposts\b.{0,40}\bsocial media\b', sentence, re.I)), social)
    elif re.search(r'\bsecurity\b', title, re.I) and (security := next((
            p for p in blocks if re.search(r'\b(?:probe|test|harden)\b.{0,100}\bAI (?:systems|models)\b', p, re.I)), '')):
        label = 'AI security testing'
        quote = next((sentence for sentence in re.split(r'(?<=[.!?])\s+', security)
                      if re.search(r'\b(?:probe|test|harden)\b', sentence, re.I)), security)
    elif action or re.search(r'\bAI (?:trainer|training|evaluator|tutor)\b', title, re.I):
        label, quote = 'AI training & evaluation', action
    else:
        label, quote = '', ''
    if quote:
        if quote.startswith('- '):
            quote = re.split(r'\s+-\s+', quote[2:], maxsplit=1)[0]
        # A verbatim excerpt, never a generated summary. Keep sentences when short.
        quote = re.split(r'(?<=[.!?])\s+(?=[A-Z])', quote)[0]
        if len(quote) > 210:
            quote = quote[:209].rsplit(' ', 1)[0].rstrip(' ,;:') + '…'
    return label, quote


def dataforce_pay_text(text):
    """Quote the project's labelled pay section when numeric normalization cannot.

    Keep complete conditional sentences (including staged task bonuses), not
    isolated money-like numbers from methods, fees or unrelated site content.
    """
    from wahojobs.candidate_source_display import plain
    active, paragraphs = False, []
    for block in text.split('\n\n'):
        if re.match(r'^#{1,6}\s', block):
            label = plain(block).strip('#: ?').casefold()
            active = bool(re.fullmatch(r'quel est le montant de la rémunération|'
                r'how much (?:will i|do i|can i) (?:get paid|earn)|compensation|remuneration|payment|reward', label))
            continue
        if active and re.search(r'[$€£]|\b(?:USD|EUR|GBP)\b', block) and re.search(r'\d', block):
            paragraphs.append(plain(block))
    quote = ' '.join(paragraphs)
    return quote if 0 < len(quote) <= 900 else None
