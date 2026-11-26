"""Diagnóstico de indexação: sitemaps enviados e inspeção de URLs (Search Console)."""
import os, json, sys, urllib.parse
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
cred = service_account.Credentials.from_service_account_info(json.loads(os.environ['GSC_SA_JSON']), scopes=['https://www.googleapis.com/auth/webmasters.readonly'])
s = AuthorizedSession(cred); site = 'sc-domain:geozion.com.br'; q = urllib.parse.quote(site, safe='')
r = s.get(f'https://searchconsole.googleapis.com/webmasters/v3/sites/{q}/sitemaps'); print('SITEMAPS', r.status_code)
for x in r.json().get('sitemap', []): print(' ', x.get('path'), x.get('lastSubmitted'), x.get('lastDownloaded'), x.get('isPending'), x.get('errors'), x.get('warnings'), x.get('contents'))
urls = sys.argv[1:] or ['https://geozion.com.br/', 'https://geozion.com.br/english/', 'https://geozion.com.br/english/data/soybeans/', 'https://geozion.com.br/dados/soja/', 'https://geozion.com.br/english/blog/', 'https://geozion.com.br/english/guides/']
for u in urls:
    r = s.post('https://searchconsole.googleapis.com/v1/urlInspection/index:inspect', json={'inspectionUrl': u, 'siteUrl': site, 'languageCode': 'pt-BR'})
    if r.status_code != 200: print('INSPECT', u, r.status_code, r.text[:200]); continue
    i = r.json()['inspectionResult'].get('indexStatusResult', {})
    print('INSPECT', u, '|', i.get('verdict'), '|', i.get('coverageState'), '|', i.get('lastCrawlTime'), '| canon google:', i.get('googleCanonical'), '| user:', i.get('userCanonical'), '| sitemap:', i.get('sitemap'), '| ref:', (i.get('referringUrls') or [])[:2])
