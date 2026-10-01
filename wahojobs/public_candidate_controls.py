"""Static, source-only tracking controls. No identity or personal state here."""
from html import escape

SCRIPT = "<script defer src='/candidate-client.js'></script>"
CSS = """.candidate-tracking{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}
.candidate-tracking button{font:inherit;cursor:pointer;border:1px solid #0c7792;border-radius:6px;padding:7px 12px;background:#fff;color:#07586d}
.candidate-tracking button:focus-visible{outline:3px solid #04313c;outline-offset:3px}
.candidate-tracking button:disabled{opacity:.6}.candidate-tracking [role=status]{flex-basis:100%}
.candidate-tracking a{font-size:14px}.candidate-hidden-notice{padding:12px;border:1px solid #ccc}
"""


def controls(job, return_to):
    canonical, variant = int(job['canonical_opportunity_id']), int(job['job_id'])
    return (f"<div class='candidate-tracking' data-candidate-canonical='{canonical}' "
        f"data-candidate-variant='{variant}' data-candidate-return='{escape(return_to,quote=True)}'>"
        "<button type='button' data-candidate-action='save'>Save</button>"
        "<button type='button' data-candidate-action='applied'>Mark as applied</button>"
        "<button type='button' data-candidate-action='not_interested'>Not interested</button>"
        "<span role='status' aria-live='polite'></span><a href='/my-jobs'>My Jobs</a></div>")
