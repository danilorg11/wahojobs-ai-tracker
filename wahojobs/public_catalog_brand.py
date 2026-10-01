"""Public catalog shell; assets are owned by the existing website, not the relay.

Palette and Poppins files come from the website globals/header. Keep this opt-in
so authenticated and legacy renderers retain their existing presentation.
"""

from wahojobs.public_catalog_analytics import HEAD as ANALYTICS_HEAD

FAVICON = "<link rel='icon' href='/favicon.ico' sizes='any'>"
NAVIGATION = ("<nav aria-label='Main'><a href='/'>Home</a>"
              "<a href='/jobs' aria-current='location'>AI Training Jobs</a>"
              "<a href='/blog'>Blog</a></nav>")
HEADER = ("<header class='site-header public-brand-header'>"
          "<a class='brand' href='/' aria-label='Wahojobs home'>"
          "<img src='/assets/images/wahojobsLogo.png' alt='Wahojobs' width='193' height='40'></a>"
          + NAVIGATION + "</header>")

# Same-origin images/fonts only. No asset proxy or private route is introduced.
ASSET_CSP = "img-src 'self'; font-src 'self'; "

CSS = """
@font-face {font-family:'Wahojobs Poppins';src:url('/assets/fonts/Poppins-Regular.ttf') format('truetype');font-weight:400;font-style:normal;font-display:swap;}
@font-face {font-family:'Wahojobs Poppins';src:url('/assets/fonts/Poppins-Bold.ttf') format('truetype');font-weight:700;font-style:normal;font-display:swap;}
:root,body {font-family:'Wahojobs Poppins',Arial,ui-sans-serif,system-ui,sans-serif;color:#11181c;background:#fafafa;}
a {color:#0c7792;}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible {outline:3px solid #04313c;outline-offset:4px;}
.site-header.public-brand-header {width:100%;max-width:none;min-height:100px;margin:0 0 24px;padding:24px clamp(16px,3.1vw,40px);background:#05a2c2;display:flex;flex-direction:row;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px 24px;}
.public-brand-header .brand {display:inline-flex;flex-shrink:0;align-items:center;min-height:44px;}
.public-brand-header .brand img {display:block;width:193px;height:40px;max-width:100%;object-fit:contain;}
.public-brand-header nav {display:flex;flex-wrap:wrap;align-items:center;gap:8px 40px;}
.public-brand-header nav a {display:inline-flex;align-items:center;min-height:44px;color:#fff;text-decoration:none;font-size:1.5rem;font-weight:400;}
.public-brand-header nav a[aria-current] {text-decoration:underline;text-decoration-thickness:3px;text-underline-offset:8px;}
.public-brand-header nav a:hover {text-decoration:underline;text-underline-offset:8px;}
h1 {line-height:1.15;}
.hero,.catalog-hero,.catalog-filters,.job-card,.job-description,.empty-results,.pagination,.eligibility-details {border-color:#dfe3e6;}
.catalog-filters,.fact {background:#f8f9fa;}
.catalog-filters label,.catalog-summary,.company-line,.job-summary,.job-location,.catalog-hero>p:last-child {color:#3e454a;}
.job-card h2 a {color:#11181c;}
.job-company,.eyebrow,.job-pay,.eligibility-details summary,.secondary-filters summary {color:#0c7792;}
.muted,.location-help,.fact dt,.verification-footer,.empty-results p,.pagination {color:#687076;}
.role-chips li,.card-attributes li,.active-filter,.view-job,.button-secondary {background:#e7f9fb;color:#04313c;border-color:#aadee6;}
.filter-actions button,.button-primary {background:#0c7792;color:#fff;}
.filter-actions button:hover,.button-primary:hover {background:#04313c;}
.public-message {max-width:960px;}
@media(max-width:680px) {
 .site-header.public-brand-header {min-height:0;padding:16px;gap:8px 20px;}
 .public-brand-header .brand img {width:154.4px;height:32px;}
 .public-brand-header nav {gap:8px 28px;}
 .public-brand-header nav a {font-size:1.125rem;}
}
"""
