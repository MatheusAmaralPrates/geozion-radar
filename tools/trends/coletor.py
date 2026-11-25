"""Monitor de buscas GeoZion: coleta o Google Trends uma vez por semana e grava no Cloudflare KV.

Roda no GitHub Actions (.github/workflows/trends.yml), segunda de manhã. Para cada grupo de config/trends.json:
  1. interesse nos últimos 12 meses, semana a semana (0–100, normalizado dentro do grupo, com a âncora do idioma);
  2. interesse por estado (Brasil para PT, EUA para EN);
  3. buscas relacionadas de cada termo: as mais buscadas e as que mais subiram.
Grava trends:snap (o painel /admin/ lê por /api/admin/trends) e acrescenta a semana em trends:hist.
Se um grupo falhar (o Google limita requisições), mantém os dados da semana anterior daquele grupo, marcados como antigos.

Variáveis: CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_ID (as mesmas do radar). Teste local: python tools/trends/coletor.py --seco
"""
import json, os, random, statistics, sys, time, urllib.error, urllib.request
import requests

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG = json.load(open(os.path.join(RAIZ, 'config', 'trends.json'), encoding='utf-8'))
SECO = '--seco' in sys.argv
BASE = 'https://trends.google.com/trends/api/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'


def kv(metodo, chave, corpo=None):
    acc, ns, tok = os.environ['CF_ACCOUNT_ID'], os.environ['CF_KV_ID'], os.environ['CF_API_TOKEN']
    url = f'https://api.cloudflare.com/client/v4/accounts/{acc}/storage/kv/namespaces/{ns}/values/{chave}'
    req = urllib.request.Request(url, method=metodo, data=corpo.encode() if corpo is not None else None,
                                 headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'text/plain'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404: return None
        raise


class Trends:
    def __init__(self, hl, tz, geo):
        self.hl, self.tz, self.s = hl, tz, requests.Session()
        self.s.headers.update({'User-Agent': UA, 'Accept-Language': hl + ',en;q=0.8'})
        self.cookies(geo)

    def cookies(self, geo):
        for _ in range(3):
            try:
                self.s.get('https://trends.google.com/trends/explore', params={'geo': geo, 'hl': self.hl}, timeout=30)
                if self.s.cookies.get('NID'): return
            except requests.RequestException: pass
            time.sleep(5)

    def get(self, path, params):
        espera = 30
        for tentativa in range(5):
            time.sleep(random.uniform(6, 12))
            try:
                r = self.s.get(BASE + path, params={'hl': self.hl, 'tz': self.tz, **params}, timeout=40)
            except requests.RequestException as e:
                print(f'  rede: {type(e).__name__}'); r = None
            if r is not None and r.status_code == 200:
                t = r.text
                return json.loads(t[t.find('{'):])
            cod = r.status_code if r is not None else '-'
            print(f'  {path}: HTTP {cod} (tentativa {tentativa + 1}); espero {espera}s')
            time.sleep(espera); espera = min(espera * 2, 300)
            if cod == 429 and tentativa == 1: self.s.cookies.clear(); self.cookies('')
        raise RuntimeError(f'{path} falhou')

    def grupo(self, termos, geo):
        req = {'comparisonItem': [{'keyword': k, 'geo': geo, 'time': 'today 12-m'} for k in termos], 'category': 0, 'property': ''}
        ws = self.get('explore', {'req': json.dumps(req, ensure_ascii=False)})['widgets']
        w = {x['id']: x for x in ws if x['id'] in ('TIMESERIES', 'GEO_MAP')}
        linha = self.get('widgetdata/multiline', {'req': json.dumps(w['TIMESERIES']['request'], ensure_ascii=False), 'token': w['TIMESERIES']['token']})
        pontos = [p for p in linha['default']['timelineData']]
        datas = [time.strftime('%Y-%m-%d', time.gmtime(int(p['time']))) for p in pontos]
        parcial = bool(pontos and pontos[-1].get('isPartial'))
        series = {k: [p['value'][i] for p in pontos] for i, k in enumerate(termos)}
        regioes = {}
        if 'GEO_MAP' in w:
            g = dict(w['GEO_MAP']['request']); g['resolution'] = 'REGION'; g['includeLowSearchVolumeGeos'] = False
            try:
                mapa = self.get('widgetdata/comparedgeo', {'req': json.dumps(g, ensure_ascii=False), 'token': w['GEO_MAP']['token']})
                for d in mapa['default']['geoMapData']:
                    uf = d.get('geoCode', '').split('-')[-1] or d.get('geoName')
                    for i, k in enumerate(termos):
                        if d.get('hasData', [True] * len(termos))[i] and d['value'][i]:
                            regioes.setdefault(k, {})[uf] = d['value'][i]
            except RuntimeError as e: print('  mapa:', e)
        rel = {}
        for x in ws:
            if x['id'] != 'RELATED_QUERIES': continue
            try:
                k = x['request']['restriction']['complexKeywordsRestriction']['keyword'][0]['value']
                d = self.get('widgetdata/relatedsearches', {'req': json.dumps(x['request'], ensure_ascii=False), 'token': x['token']})
                listas = d['default']['rankedList']
                fmt = lambda L: [[q['query'], q.get('formattedValue') or q['value']] for q in L.get('rankedKeyword', [])[:12]]
                rel[k] = {'top': fmt(listas[0]) if listas else [], 'subindo': fmt(listas[1]) if len(listas) > 1 else []}
            except (RuntimeError, KeyError, IndexError) as e: print('  relacionadas:', e)
        return datas, parcial, series, regioes, rel


def metricas(serie, parcial):
    s = serie[:-1] if parcial and len(serie) > 1 else serie
    if not s: return {}
    ult4, ant4 = s[-4:], s[-8:-4]
    m4, a4 = statistics.mean(ult4), (statistics.mean(ant4) if ant4 else 0)
    pico = max(s); ip = s.index(pico)
    return {'ultimo': s[-1], 'media4': round(m4, 1), 'var4': round((m4 - a4) / a4 * 100, 1) if a4 else None,
            'media52': round(statistics.mean(s), 1), 'pico': pico, 'picoIdx': ip,
            'vsMedia': round((m4 - statistics.mean(s)) / statistics.mean(s) * 100, 1) if statistics.mean(s) else None}


def coleta_idioma(lg, cfg, antes):
    t = Trends(cfg['hl'], cfg['tz'], cfg['geo'])
    anc, saida, velhos = cfg['ancora'], [], {g['id']: g for g in (antes or {}).get('grupos', [])}
    for g in cfg['grupos']:
        termos = g['termos'] + [anc]
        print(f'[{lg}] {g["nome"]}: {", ".join(termos)}')
        try:
            datas, parcial, series, regioes, rel = t.grupo(termos, cfg['geo'])
            ma = statistics.mean(series[anc][:-1] or series[anc]) or 1
            saida.append({'id': g['id'], 'nome': g['nome'], 'datas': datas, 'parcial': parcial, 'antigo': False, 'coletado': int(time.time()),
                          'termos': [{'kw': k, 'ancora': k == anc, 'serie': series[k], **metricas(series[k], parcial),
                                      'vsAncora': round(statistics.mean(series[k][:-1] or series[k]) / ma, 2),
                                      'regioes': dict(sorted(regioes.get(k, {}).items(), key=lambda x: -x[1])[:27]),
                                      'relacionadas': rel.get(k, {'top': [], 'subindo': []})} for k in termos]})
        except Exception as e:
            print(f'  grupo {g["id"]} falhou: {e}')
            if g['id'] in velhos: saida.append({**velhos[g['id']], 'antigo': True})
            t = Trends(cfg['hl'], cfg['tz'], cfg['geo']); time.sleep(60)
    # destaques: o que mais subiu nas últimas 4 semanas (ignora termos com pouco volume) e buscas em disparada
    todos = [(g, x) for g in saida for x in g['termos'] if not x['ancora'] and x.get('var4') is not None and (x.get('media52') or 0) >= 5]
    sobe = sorted(todos, key=lambda p: -p[1]['var4'])[:8]
    desce = sorted(todos, key=lambda p: p[1]['var4'])[:5]
    disparada = []
    for g in saida:
        for x in g['termos']:
            for q, v in x['relacionadas'].get('subindo', []):
                if str(v).lower() in ('breakout', 'aumento repentino', 'excelente') or (isinstance(v, str) and v.replace('+', '').replace('%', '').replace('.', '').replace(',', '').isdigit() and int(v.replace('+', '').replace('%', '').replace('.', '').replace(',', '')) >= 500):
                    disparada.append({'busca': q, 'valor': v, 'termo': x['kw'], 'grupo': g['nome']})
    return {'geo': cfg['geo'], 'ancora': anc, 'grupos': saida,
            'destaques': {'sobe': [{'kw': x['kw'], 'grupo': g['nome'], 'var4': x['var4'], 'ultimo': x['ultimo']} for g, x in sobe],
                          'desce': [{'kw': x['kw'], 'grupo': g['nome'], 'var4': x['var4'], 'ultimo': x['ultimo']} for g, x in desce],
                          'disparada': disparada[:20]}}


def main():
    antes = None
    if not SECO:
        try: antes = json.loads(kv('GET', 'trends:snap') or 'null')
        except Exception as e: print('KV leitura:', e)
    snap = {'atualizado': int(time.time()), 'semana': time.strftime('%Y-%m-%d'), 'fonte': 'Google Trends', 'periodo': 'últimos 12 meses, semanal'}
    for lg in ('pt', 'en'):
        snap[lg] = coleta_idioma(lg, CFG[lg], (antes or {}).get(lg))
    ok = sum(1 for lg in ('pt', 'en') for g in snap[lg]['grupos'] if not g['antigo'])
    tot = len(CFG['pt']['grupos']) + len(CFG['en']['grupos'])
    print(f'{ok}/{tot} grupos atualizados')
    if SECO:
        print(json.dumps(snap, ensure_ascii=False)[:3000]); return
    if ok == 0 and antes:
        print('nenhum grupo novo: mantenho a foto anterior'); sys.exit(1)
    kv('PUT', 'trends:snap', json.dumps(snap, ensure_ascii=False, separators=(',', ':')))
    try: hist = json.loads(kv('GET', 'trends:hist') or '[]')
    except Exception: hist = []
    hist = [h for h in hist if h.get('semana') != snap['semana']]
    hist.append({'semana': snap['semana'], **{lg: {x['kw']: x.get('ultimo') for g in snap[lg]['grupos'] if not g['antigo'] for x in g['termos']} for lg in ('pt', 'en')}})
    kv('PUT', 'trends:hist', json.dumps(hist[-260:], ensure_ascii=False, separators=(',', ':')))
    print('gravado em trends:snap e trends:hist')
    if ok < tot: sys.exit(0)


if __name__ == '__main__':
    main()
