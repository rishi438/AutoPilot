"""Constants"""

LLM_REASONING_TIMEOUTS = {"low": 190, "medium": 310, "high": 610}

CSP_SWAGGER_DOCS_SCRIPT_SOURCE = (
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
)
CSP_SCRIPT_SOURCE_TEMPLATE = (
    "script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net "
    "https://us-assets.i.posthog.com https://eu-assets.i.posthog.com"
)
CSP_STYLE_SOURCE_TEMPLATE = (
    "style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com "
    "https://cdn.jsdelivr.net"
)
CSP_STATIC_DIRECTIVES = (
    "default-src 'self'",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "connect-src 'self' wss: https://us.i.posthog.com https://eu.i.posthog.com",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
)

_INVALID_JOB_TITLES = frozenset(
    {"unknown", "n/a", "na", "none", "null", "job application"}
)
