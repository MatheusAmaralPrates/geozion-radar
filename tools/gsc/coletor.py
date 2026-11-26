"""Google Search Console -> KV (gsc:snap), para o painel admin do site.

Lê impressões, cliques, CTR e posição média do geozion.com.br pela API do Search Console,
com uma conta de serviço (JSON na variável GSC_SA_JSON) adicionada como usuária da propriedade.
Separa o total, o site em português e o site em inglês (/en/ e /english/).
Variáveis: GSC_SA_JSON, CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_ID. Teste local: python tools/gsc/coletor.py --seco
"""
import os, sys, json, time, datetime as dt, urllib.request, urllib.error, urllib.parse
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

SCOPE = ['https://www.googleapis.com/auth/webmasters.readonly']
API = 'https://searchconsole.googleapis.com/webmasters/v3'
EN_RE = r'geozion\.com\.br/(en|english)(/|$)'
SECO = '--seco' in sys.argv

def kv(metodo, chave, corpo=None):
    acc, ns, tok = os.environ['CF_ACCOUNT_ID'], os.environ['CF_KV_ID'], os.environ['CF_API_TOKEN']
    url = f'https://api.cloudflare.com/client/v4/accounts/{acc}/storage/kv/namespaces/{ns}/values/{chave}'
    req = urllib.request.Request(url, method=metodo, data=corpo.encode() if corpo is not None else None,
                                 headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'text/plain'})
    with urllib.request.urlopen(req, timeout=30) as r: return r.read().decode()

def sessao():
    info = json.loads(os.environ['GSC_SA_JSON'])
    cred = service_account.Credentials.from_service_account_info(info, scopes=SCOPE)
    return AuthorizedSession(cred), info.get('client_email')

def propriedade(s):
    r = s.get(API + '/sites'); r.raise_for_status()
    sites = [x['siteUrl'] for x in r.json().get('siteEntry', []) if x.get('permissionLevel') != 'siteUnverifiedUser']
    for alvo in ('sc-domain:geozion.com.br', 'https://geozion.com.br/', 'https://www.geozion.com.br/'):
        if alvo in sites: return alvo, sites
    return None, sites

def consulta(s, site, ini, fim, dims, lang=None, limite=1000):
    corpo = {'startDate': ini, 'endDate': fim, 'dimensions': dims, 'rowLimit': limite, 'dataState': 'all'}
    if lang:
        corpo['dimensionFilterGroups'] = [{'filters': [{'dimension': 'page', 'operator': 'includingRegex' if lang == 'en' else 'excludingRegex', 'expression': EN_RE}]}]
    for tent in range(4):
        r = s.post(f"{API}/sites/{urllib.parse.quote(site, safe='')}/searchAnalytics/query", json=corpo)
        if r.status_code in (429, 500, 503): time.sleep(5 * (tent + 1)); continue
        r.raise_for_status(); return r.json().get('rows', [])
    r.raise_for_status()

def tot(rows):
    i = sum(x['impressions'] for x in rows); c = sum(x['clicks'] for x in rows)
    pos = sum(x['position'] * x['impressions'] for x in rows) / i if i else None
    return {'imp': i, 'cli': c, 'ctr': c / i if i else None, 'pos': round(pos, 1) if pos else None}

def linhas(rows, chave):
    return [{chave: x['keys'][0], 'imp': x['impressions'], 'cli': x['clicks'], 'ctr': round(x['ctr'], 4), 'pos': round(x['position'], 1)} for x in rows]

def main():
    s, email = sessao()
    site, sites = propriedade(s)
    if not site:
        raise SystemExit(f'A conta de serviço {email} não vê a propriedade geozion.com.br no Search Console (vê: {sites}). '
                         'Adicione esse e-mail como usuário em Search Console > Configurações > Usuários e permissões.')
    hoje = dt.date.today(); fim = hoje  # dataState=all já traz os dias recentes (preliminares)
    d = lambda n: (fim - dt.timedelta(days=n)).isoformat()
    ini90, ini28, ini56 = d(89), d(27), d(55)
    out = {'gerado': int(time.time()), 'site': site, 'fim': fim.isoformat(), 'janela': [ini28, fim.isoformat()], 'anterior': [ini56, d(28)]}
    for lg in ('todos', 'pt', 'en'):
        f = None if lg == 'todos' else lg
        dia = consulta(s, site, ini90, fim.isoformat(), ['date'], f)
        cur = [x for x in dia if x['keys'][0] >= ini28]; ant = [x for x in dia if d(55) <= x['keys'][0] <= d(28)]
        out[lg] = {
            'atual': tot(cur), 'anterior': tot(ant),
            'dia': [[x['keys'][0], x['impressions'], x['clicks'], round(x['position'], 1)] for x in dia],
            'buscas': linhas(consulta(s, site, ini28, fim.isoformat(), ['query'], f, 50), 'q'),
            'paginas': linhas(consulta(s, site, ini28, fim.isoformat(), ['page'], f, 30), 'p'),
            'paises': linhas(consulta(s, site, ini28, fim.isoformat(), ['country'], f, 15), 'c'),
            'aparelhos': linhas(consulta(s, site, ini28, fim.isoformat(), ['device'], f, 5), 'd'),
        }
    total = consulta(s, site, '2020-01-01', fim.isoformat(), ['date'])
    out['desde'] = total[0]['keys'][0] if total else None
    out['acumulado'] = tot(total)
    txt = json.dumps(out, ensure_ascii=False, separators=(',', ':'))
    print(f"{site}: 28d {out['todos']['atual']} · anterior {out['todos']['anterior']} · acumulado {out['acumulado']} desde {out['desde']} · dias {len(out['todos']['dia'])} · {len(txt)} bytes")
    if SECO: print(txt[:1500]); return
    kv('PUT', 'gsc:snap', txt)

if __name__ == '__main__':
    main()
