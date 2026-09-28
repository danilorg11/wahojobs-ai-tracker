"""V3 individual authority for observed DataForce remote contributor families.

This reader does not change the pinned V1 or Thyme V2 contracts. A current index
card, its own canonical detail, paid remote task facts, and the detail's own
application action are all required. A listing is never closed by absence.
"""
from dataclasses import replace
from hashlib import sha256
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import parse_qs, urlparse

from wahojobs.classification import (AVAILABILITY_BASIS_PUBLIC_PAGE,
    OPPORTUNITY_KIND_LIVE_POSTING, OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY)
from wahojobs.crawler.types import BODY_OBSERVATION_PRESENT, RecordPromotionAttestation

CONTRACT_ID = 'dataforce_remote_contributor_family_v3'
ORIGIN = 'https://dataforcecommunity.transperfect.com'
_VOID = frozenset(('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'))
# Public role-specific actions observed in the retained September 24 journal.
# New same-family identities can qualify, but cannot borrow another known role's
# application action. An existing role changing its action needs fresh review.
_KNOWN_APPLICATIONS = {
    '/project/cadence-evaluation-project-pa':'P2Hr1LsqBd',
    '/project/cadence-evaluation-project-mx':'TGXUuLaYuA',
    '/project/cadence-evaluation-project-fr':'B42clmySU2',
    '/project/cadence-evaluation-project-nl':'-k6bcgg6LE',
    '/project/cadence-evaluation-project-jp':'xRLPFOf1Fy',
    '/project/cadence-evaluation-project-de':'pAWrU5bwgO',
    '/project/cadence-evaluation-project-es':'0HgNBmi_6r',
    '/project/cadence-evaluation-project-br':'VMa3uWBvBG',
    '/project/cadence-evaluation-project-be':'osZa0gIBGq',
    '/project/cadence-evaluation-project-pl':'fbyg7GfWBp',
    '/project/ronia-remote-photo-collection-japan':'3rjE7GSlcx',
    '/project/ronia-remote-photo-collection-mexico':'qy1dlRS8Gm',
    '/project/ronia-remote-photo-collection-thailand':'KrV4yzp3b7',
    '/project/ronia-remote-photo-collection-south-africa':'tgoyiXlp4m',
    '/project/ronia-remote-photo-collection-italy':'Lx1101FpPn',
    '/project/ronia-remote-photo-collection-denmark':'3EGVOCnA8U',
    '/project/ronia-remote-photo-collection-turkey':'VktsrE0qwv',
    '/project/ronia-remote-photo-collection-united-states':'3eKNP07QZA',
    '/project/gardenia-speech-collection-fr-non-native':'dHeo_5rAK7',
    '/project/gardenia-speech-collection-us-non-native':'AN6MFaI_gi',
    '/project/gardenia-speech-collection-france':'t51zOOD19V',
    '/project/gardenia-speech-collection-us':'6KDwU5yaUR',
    '/project/triton-france':'0TjQTKwzGd',
    '/project/voice-talent-casting-silkroad-mulberry':'07nDdhd2L4',
}


def _text(value):
    return ' '.join(value.split())


def _fold(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c)).replace('\u2019', "'").replace('\u2013', '-')


def supported_family(job):
    """Recognize evidenced families, not arbitrary remote cards or role IDs."""
    parsed = urlparse(job.url)
    title = getattr(job, 'title', '') or ''
    if (parsed.scheme != 'https' or parsed.netloc != 'dataforcecommunity.transperfect.com'
            or parsed.query or parsed.fragment or not re.fullmatch(r'/project/[a-z0-9-]+', parsed.path)
            or re.search(r'\b(?:minor\w*|menor\w*|on[ -]?site)\b', _fold(title))):
        return None
    family = None
    if re.fullmatch(r'/project/cadence-evaluation-project-[a-z0-9-]+', parsed.path) and title.startswith('Cadence Evaluation Project - '):
        family, category = 'cadence', 'Text'
    elif re.fullmatch(r'/project/ronia-remote-photo-collection-[a-z0-9-]+', parsed.path) and title.startswith('Ronia Raw Remote Photo Collection - '):
        family, category = 'ronia', 'Photo'
    elif re.fullmatch(r'/project/gardenia-speech-collection-[a-z0-9-]+', parsed.path) and title.startswith('Gardenia Speech Collection - '):
        family, category = 'gardenia', 'Audio'
    elif parsed.path == '/project/triton-france' and title == 'Triton - Speech Collection - France':
        family, category = 'triton', 'Audio'
    elif parsed.path == '/project/voice-talent-casting-silkroad-mulberry' and title == 'Voice Talent Casting - Silkwood & Mulberry':
        family, category = 'tts_casting', 'Audio'
    if family is None:
        return None
    metadata = job.source_metadata or {}
    country = metadata.get('Country') or ''
    if (job.external_id != 'dataforce::'+parsed.path.lstrip('/') or job.commitment != 'Remote'
            or metadata.get('Type') != 'Remote' or job.department != category or job.expertise != category
            or metadata.get('City') or job.location != (country or 'Remote')
            or (family != 'tts_casting' and not country)
            or (family == 'ronia' and title != 'Ronia Raw Remote Photo Collection - '+country)
            or (family == 'triton' and country != 'France')):
        return None
    return family


class _Detail(HTMLParser):
    """Visible role body and application inside the single main element only."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]; self.canonicals=[]; self.main_count=0; self.body_count=0
        self.body=[]; self.headings=[]; self.actions=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        parent=self.stack[-1] if self.stack else dict(main=False,body=False,hidden=False)
        main=parent['main'] or tag=='main'
        if tag=='main': self.main_count+=1
        style=re.sub(r'\s+', '', attrs.get('style','').casefold())
        hidden=(parent['hidden'] or tag in ('style','script','template','noscript')
            or 'hidden' in attrs or 'inert' in attrs or attrs.get('aria-hidden','').casefold()=='true'
            or 'display:none' in style or 'visibility:hidden' in style)
        is_body=main and not hidden and 'field--name-body' in attrs.get('class','').split()
        if is_body:self.body_count+=1
        body=parent['body'] or is_body
        if tag=='link' and 'canonical' in attrs.get('rel','').casefold().split():
            self.canonicals.append(attrs.get('href'))
        heading=[] if tag=='h1' and body and not hidden else None
        action=[attrs.get('href'),[]] if tag=='a' and main and not hidden else None
        if heading is not None:self.headings.append(heading)
        if action is not None:self.actions.append(action)
        if tag not in _VOID:
            self.stack.append(dict(tag=tag,main=main,body=body,hidden=hidden,heading=heading,action=action))

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index]['tag']==tag:
                del self.stack[index:];break

    def handle_data(self, value):
        if not self.stack or self.stack[-1]['hidden']:return
        if self.stack[-1]['body']:self.body.append(value)
        for item in self.stack:
            if item.get('heading') is not None:item['heading'].append(value)
            if item.get('action') is not None:item['action'][1].append(value)


def _application(page, role_url):
    values=[]
    for href, words in page.actions:
        label=_fold(_text(' '.join(words)))
        if label not in ('apply here','postuler ici'):continue
        url=''.join((href or '').split()).rstrip('&')
        parsed=urlparse(url);query=parse_qs(parsed.query,keep_blank_values=True)
        tokens=query.get('registration-type',[])
        if (parsed.scheme!='https' or parsed.netloc!='hub.transperfect.com'
                or parsed.path not in ('/','/registration') or parsed.fragment
                or set(query)!={'registration-type'} or len(tokens)!=1
                or re.fullmatch(r'[A-Za-z0-9_-]{8,80}',tokens[0]) is None):
            raise ValueError('DataForce family application action is outside its contract.')
        path=urlparse(role_url).path
        if ((path in _KNOWN_APPLICATIONS and tokens[0]!=_KNOWN_APPLICATIONS[path])
                or any(tokens[0]==token and known_path!=path for known_path,token in _KNOWN_APPLICATIONS.items())):
            raise ValueError('DataForce family application belongs to a different observed role.')
        values.append(url)
    if len(set(values))!=1:
        raise ValueError('DataForce family application action is missing or conflicting.')
    return values[0]


def detail_evidence(index, detail_html):
    family=supported_family(index)
    if family is None:raise ValueError('DataForce remote contributor family is unqualified.')
    page=_Detail();page.feed(detail_html);page.close()
    if page.main_count!=1 or page.body_count!=1 or page.canonicals!=[index.url]:
        raise ValueError('DataForce family canonical or main role identity is inconsistent.')
    headings=[_text(' '.join(parts)) for parts in page.headings if _text(' '.join(parts))]
    expected=index.title
    if family=='ronia':expected=expected.replace('Ronia Raw Remote Photo Collection - ','Ronia Raw Photo Collection - ',1)
    if (family=='gardenia' and urlparse(index.url).path=='/project/gardenia-speech-collection-france'
            and index.title=='Gardenia Speech Collection - France' and index.location=='France'):
        expected='Collecte de données vocales – France et outre-mer'
    if headings!=[expected]:
        raise ValueError('DataForce family exact detail heading disagrees with its card.')
    text=_fold(_text(' '.join(page.body)))
    if any(word in text for word in ('captcha','access denied','verify you are human','no longer accepting')):
        raise ValueError('DataForce family page is not an open application observation.')
    if re.search(r'\b(?:onsite|on-site|on site)\b',text):
        raise ValueError('DataForce family detail contradicts remote work.')
    # Cadence recruits qualified educators who teach children; the students'
    # ages are not applicant ages. The other evidenced families explicitly
    # require adult participants. Do not infer eligibility from task keywords.
    if (re.search(r'\b(?:child(?:ren)?[ -]only|minor(?:s)?[ -]only)\b',text)
            or re.search(r'\b(?:participants?|applicants?|contributors?)\s+(?:aged?\s+)?(?:under\s*18|minors?|children)\b',text)
            or re.search(r'\b(?:participants?|applicants?|contributors?|you)\s+(?:must|should|can|may)\s+be\s+(?:under\s*18|minors?|children)\b',text)
            or re.search(r'\b(?:minors?|children)\s+(?:are\s+)?(?:eligible|welcome|may participate|can participate)\b',text)):
        raise ValueError('DataForce family detail contradicts adult contributor eligibility.')
    if family=='cadence':
        eligible=('be qualified as a middle or high school educator' in text
                  and 'have experience in teaching whole classes' in text)
    else:
        eligible=any(fragment in text for fragment in
                     ('be 18 years or older','avoir 18 ans ou plus','age 18+'))
    if not eligible:
        raise ValueError('DataForce family contributor eligibility is unqualified.')
    remote=('this is a fully remote project' in text or 'projet entierement a distance' in text)
    paid=(('you will receive' in text and 'compensation' in text and 'usd' in text)
          or ('vous recevrez' in text and 'remuneration' in text and 'usd' in text))
    country=_fold((index.source_metadata or {}).get('Country') or '')
    if family=='tts_casting':
        remote='professional-quality home studio' in text
        paid='project compensation, including recording fee' in text
        work=all(fragment in text for fragment in ('text-to-speech (tts) technology',
            'training and testing existing tts models','voice samples','sample submissions are not compensated'))
    else:
        residence=any(fragment in text for fragment in (
            'resident of '+country,'resident of the '+country,'live in '+country,'live in the '+country,
            'within '+country,'within the '+country,'vivre en '+country,
            'region de '+country,'en '+country))
        if not country or not residence:
            raise ValueError('DataForce family detail lacks matching country evidence.')
        if family=='cadence':
            paid='compensation is task-based' in text and 'will be paid' in text
            work=all(fragment in text for fragment in ('evaluate educational content',
                'multilingual learning application','rate and rank','independent contractor'))
        elif family=='ronia':
            work=all(fragment in text for fragment in ('raw photo collection',
                'advanced ai and computer vision technologies','submitted and accepted photos'))
        else:
            work=(('voice assistant' in text and 'record' in text)
                  or ('assistant vocal' in text and 'enregistrements' in text)) and family in text
    if not (remote and paid and work):
        raise ValueError('DataForce family paid remote task evidence is incomplete.')
    return dict(family=family,application_url=_application(page,index.url))


def qualify_record(index, detail_html):
    role=detail_evidence(index,detail_html)
    metadata=dict(index.source_metadata or {})
    if not {'index_card_html','index_page_url','index_page_sha256'}<=set(metadata):
        raise ValueError('DataForce family exact index provenance is missing.')
    metadata.update(application_url=role['application_url'],dataforce_remote_family=role['family'])
    casting=role['family']=='tts_casting'
    return replace(index,source_body=detail_html,source_body_format='text/html',source_metadata=metadata,
        opportunity_kind=OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY if casting else OPPORTUNITY_KIND_LIVE_POSTING,
        availability_basis=AVAILABILITY_BASIS_PUBLIC_PAGE,include_in_live_market_estimate=not casting,
        record_promotion_attestation=RecordPromotionAttestation(CONTRACT_ID,BODY_OBSERVATION_PRESENT,
            dict(role,external_id=index.external_id,title=index.title,detail_url=index.url,
                index_page_url=metadata['index_page_url'],index_page_sha256=metadata['index_page_sha256'],
                index_card_sha256=sha256(metadata['index_card_html'].encode()).hexdigest(),
                detail_sha256=sha256(detail_html.replace('\r\n','\n').replace('\r','\n').strip().encode()).hexdigest())))
