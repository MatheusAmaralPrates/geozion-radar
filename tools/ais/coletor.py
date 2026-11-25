"""Radar marítimo GeoZion: coleta AIS (aisstream.io) e grava a foto das áreas no Cloudflare KV.

Roda no GitHub Actions a cada ~15 minutos (.github/workflows/ais.yml). Em cada rodada:
  1. escuta o aisstream.io por DURACAO segundos nas áreas de config/areas_ais.json;
  2. junta com os navios vistos nos últimos 90 minutos (rodada anterior);
  3. mede, por área: fundeados (fila), atracados, em trânsito; compara com a mediana de 7 dias;
  4. gera alertas (fila acima do normal, tráfego abaixo do normal em gargalos, área sem sinal);
  5. grava ais:snap e ais:hist no KV (a página lê por /api/ais).

Variáveis (GitHub › Settings › Secrets): AISSTREAM_KEY, CF_API_TOKEN (permissão Workers KV Storage: Edit),
CF_ACCOUNT_ID, CF_KV_ID (id do namespace GZ_KV). Opcional: DURACAO (padrão 180).
Uso local de teste: python tools/ais/coletor.py --seco   (escuta e imprime, sem gravar)
"""
import asyncio, json, os, statistics, sys, time, urllib.request
RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AREAS = json.load(open(os.path.join(RAIZ, 'config', 'areas_ais.json'), encoding='utf-8'))['areas']
DUR = int(os.environ.get('DURACAO') or 180)
GUARDA_MIN = 90                    # navio some do radar se não transmitir por 90 min
HIST_PONTOS = 7 * 24 * 4           # 7 dias a cada 15 min

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

def area_de(lat, lon):
    for a in AREAS:
        (s, w), (n, e) = a['bbox']
        if s <= lat <= n and w <= lon <= e: return a['id']
    return None

def tipo_navio(t):
    if t is None: return 'outro'
    if 70 <= t <= 79: return 'carga'
    if 80 <= t <= 89: return 'tanque'
    if 60 <= t <= 69: return 'passageiros'
    return 'outro'

async def escuta(navios):
    import websockets
    sub = {'APIKey': os.environ['AISSTREAM_KEY'], 'BoundingBoxes': [a['bbox'] for a in AREAS],
           'FilterMessageTypes': ['PositionReport', 'ShipStaticData', 'StandardClassBPositionReport']}
    fim, n = time.time() + DUR, 0
    async with websockets.connect('wss://stream.aisstream.io/v0/stream', compression='deflate', max_size=2**22) as ws:
        await ws.send(json.dumps(sub))
        while time.time() < fim:
            try: msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1, fim - time.time())))
            except asyncio.TimeoutError: break
            n += 1
            md, tp = msg.get('MetaData', {}), msg.get('MessageType')
            mmsi = str(md.get('MMSI') or '')
            if not mmsi: continue
            v = navios.setdefault(mmsi, {'m': mmsi})
            v['n'] = (md.get('ShipName') or v.get('n') or '').strip()
            v['lat'], v['lon'], v['t'] = round(md.get('latitude', v.get('lat', 0)), 5), round(md.get('longitude', v.get('lon', 0)), 5), int(time.time())
            if tp == 'PositionReport':
                p = msg['Message']['PositionReport']
                v['sog'], v['cog'], v['st'] = p.get('Sog'), p.get('Cog'), p.get('NavigationalStatus')
                v['hd'] = p.get('TrueHeading') if p.get('TrueHeading') not in (None, 511) else None
            elif tp == 'StandardClassBPositionReport':
                p = msg['Message']['StandardClassBPositionReport']; v['sog'], v['cog'], v['b'] = p.get('Sog'), p.get('Cog'), 1
            elif tp == 'ShipStaticData':
                s = msg['Message']['ShipStaticData']
                v['ty'] = s.get('Type'); v['dst'] = (s.get('Destination') or '').strip()[:24]; v['dr'] = s.get('MaximumStaticDraught')
                d = s.get('Dimension') or {}; v['len'] = (d.get('A') or 0) + (d.get('B') or 0) or None
                v['imo'] = s.get('ImoNumber') or None
    return n

def situacao(v):
    st, sog = v.get('st'), v.get('sog') or 0
    if st == 5: return 'atracado'
    if st == 1: return 'fundeado'
    if sog < 0.5: return 'parado'
    return 'transito'

def main():
    seco = '--seco' in sys.argv
    ant = json.loads(kv('GET', 'ais:snap') or '{}') if not seco else {}
    agora = int(time.time())
    navios = {v['m']: v for v in ant.get('navios', []) if agora - v.get('t', 0) < GUARDA_MIN * 60}
    msgs = asyncio.run(escuta(navios))
    vivos = [v for v in navios.values() if agora - v.get('t', 0) < GUARDA_MIN * 60 and v.get('lat')]
    for v in vivos: v['a'], v['s'], v['k'] = area_de(v['lat'], v['lon']), situacao(v), tipo_navio(v.get('ty'))
    vivos = [v for v in vivos if v['a'] and v['k'] != 'passageiros']
    hist = json.loads((kv('GET', 'ais:hist') if not seco else None) or '{}')
    areas, alertas = [], []
    for a in AREAS:
        L = [v for v in vivos if v['a'] == a['id'] and v['k'] in ('carga', 'tanque', 'outro')]
        com = [v for v in L if v['k'] in ('carga', 'tanque')]
        c = {'total': len(com), 'fundeados': sum(1 for v in com if v['s'] in ('fundeado', 'parado')), 'atracados': sum(1 for v in com if v['s'] == 'atracado'),
             'transito': sum(1 for v in com if v['s'] == 'transito'), 'carga': sum(1 for v in com if v['k'] == 'carga'), 'tanque': sum(1 for v in com if v['k'] == 'tanque')}
        H = hist.setdefault(a['id'], []); H.append([agora, c['total'], c['fundeados'], c['atracados'], c['transito']]); del H[:-HIST_PONTOS]
        base = H[:-1]
        med = {k: (statistics.median([h[i] for h in base]) if len(base) >= 12 else None) for k, i in (('total', 1), ('fundeados', 2), ('transito', 4))}
        al = None
        if med['total'] and med['total'] >= 5 and c['total'] == 0:
            al = {'nivel': 'sinal', 'pt': 'Sem sinal AIS nesta rodada (cobertura dos receptores)', 'en': 'No AIS signal this round (receiver coverage)'}
        elif a['tipo'] == 'porto' and med['fundeados'] is not None and c['fundeados'] >= 8 and c['fundeados'] >= 1.5 * max(med['fundeados'], 1):
            al = {'nivel': 'alto', 'pt': f"Fila acima do normal: {c['fundeados']} navios esperando (mediana de 7 dias: {med['fundeados']:.0f})", 'en': f"Queue above normal: {c['fundeados']} ships waiting (7-day median: {med['fundeados']:.0f})"}
        elif a['tipo'] == 'gargalo' and med['transito'] and med['transito'] >= 8 and c['transito'] <= 0.6 * med['transito']:
            al = {'nivel': 'alto', 'pt': f"Tráfego abaixo do normal: {c['transito']} navios em trânsito (mediana: {med['transito']:.0f})", 'en': f"Traffic below normal: {c['transito']} ships in transit (median: {med['transito']:.0f})"}
        if al: al['area'] = a['id']; alertas.append(al)
        areas.append({'id': a['id'], **c, 'med': med, 'serie': [[h[0], h[1], h[2]] for h in H[-96:]], 'alerta': al})
    snap = {'atualizado': agora, 'mensagens': msgs, 'duracao': DUR, 'areas': areas, 'alertas': alertas,
            'navios': [{k: v.get(k) for k in ('m', 'n', 'lat', 'lon', 'sog', 'cog', 'hd', 'st', 's', 'k', 'ty', 'dst', 'dr', 'len', 'imo', 'a', 't')} for v in vivos]}
    print(f'{msgs} mensagens · {len(vivos)} navios · {len(alertas)} alertas')
    if seco: print(json.dumps(areas, ensure_ascii=False)[:2000]); return
    kv('PUT', 'ais:snap', json.dumps(snap, separators=(',', ':')))
    kv('PUT', 'ais:hist', json.dumps(hist, separators=(',', ':')))

if __name__ == '__main__':
    main()
