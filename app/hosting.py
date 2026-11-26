"""Explicit opt-in for a private hosted instance behind an HTTPS ingress."""
import os
from urllib.parse import urlsplit

HOSTED = os.getenv('HOSTED_MODE', '0') == '1'
PUBLIC_ORIGIN = ''
HOSTS = ['localhost', '127.0.0.1', 'testserver', '[::1]']
if HOSTED:
    configured = os.getenv('PUBLIC_ORIGIN', '').strip().rstrip('/')
    render_host = os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()
    PUBLIC_ORIGIN = configured or ('https://' + render_host if render_host else '')
    parsed = urlsplit(PUBLIC_ORIGIN)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
        or parsed.path or parsed.query or parsed.fragment or parsed.port is not None
        or parsed.netloc != parsed.hostname or '*' in parsed.hostname):
        raise RuntimeError('Hosted mode requires a single HTTPS public hostname')
    if len(os.getenv('ADMIN_PASSWORD', '')) < 24:
        raise RuntimeError('Hosted mode requires a randomly generated ADMIN_PASSWORD of 24+ characters')
    HOSTS = [parsed.hostname]
