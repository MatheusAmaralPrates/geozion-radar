"""Mantém o radar vivo sem depender do agendamento do GitHub (que atrasa ou some em repositórios novos).

Roda o coletor a cada 15 minutos por ~5h40 dentro de um único job do GitHub Actions; no fim, o workflow
dispara a si mesmo de novo (workflow_dispatch com o GITHUB_TOKEN), formando um ciclo contínuo.
Uma rodada que falhe (rede, aisstream fora do ar) não derruba o ciclo: espera a próxima.
"""
import os, subprocess, sys, time

INTERVALO = 15 * 60
LIMITE = int(os.environ.get('CICLO_MIN') or 340) * 60      # o job tem teto de 6 h; sobra folga para encerrar e redisparar
COLETOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'coletor.py')

ini, n, falhas = time.time(), 0, 0
while time.time() - ini < LIMITE - INTERVALO + 60:
    t0 = time.time(); n += 1
    print(f'--- rodada {n} · {time.strftime("%H:%M:%S", time.gmtime())} UTC', flush=True)
    r = subprocess.run([sys.executable, COLETOR])
    if r.returncode: falhas += 1; print(f'rodada {n} falhou (código {r.returncode})', flush=True)
    resto = INTERVALO - (time.time() - t0)
    if resto > 0 and time.time() - ini + resto < LIMITE - INTERVALO + 60: time.sleep(resto)
print(f'ciclo encerrado: {n} rodadas, {falhas} com falha')
