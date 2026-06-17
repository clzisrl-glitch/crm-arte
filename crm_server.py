#!/usr/bin/env python3
"""
Arte Editoria Monete - Server locale
Avvia con: python3 crm_server.py
Poi apri: http://localhost:8080
"""
import json, os, sys, re
from pathlib import Path

# Install flask if needed
try:
    from flask import Flask, request, jsonify, send_file, send_from_directory
except ImportError:
    print("Installazione Flask...")
    os.system(f"{sys.executable} -m pip install flask -q")
    from flask import Flask, request, jsonify, send_file, send_from_directory

app = Flask(__name__)
# RAILWAY_READY
import crm_db, crm_auth
from functools import wraps
PORT = int(os.environ.get("PORT","8080"))
def _utente_corrente():
    if not crm_auth.USE_AUTH:
        return {"nome":"locale","ruolo":"titolare"}
    return crm_auth.verifica_token(request.cookies.get("crm_token",""))
def solo_titolare(azione):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u=_utente_corrente()
            if crm_auth.USE_AUTH and (not u or not crm_auth.puo_fare(u["ruolo"], azione)):
                return jsonify({"error":"Operazione riservata al titolare."}), 403
            return f(*a, **k)
        return w
    return deco


# Data file path — same folder as this script
BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / 'crm_data.json'
# Cerca il file HTML con vari nomi possibili
_html_names = ['CRM_Arte.html','CRM_NUOVO.html','CRM_FileMaker.html']
HTML_FILE = next((BASE_DIR/n for n in _html_names if (BASE_DIR/n).exists()), BASE_DIR/'CRM_Arte.html')

def load_data():
    return crm_db.load_data()

import hashlib, datetime
BACKUP_DIR = BASE_DIR / 'backup'
MAX_BACKUPS = 60   # giorni da conservare (~2 mesi): una copia al giorno
_last_backup_hash = None

def _auto_backup(text):
    """Tiene UNA copia al giorno in backup/ (un file datato per giornata,
    aggiornato con l'ultimo stato del giorno), conservando gli ultimi
    MAX_BACKUPS giorni. Salta se i dati non sono cambiati."""
    global _last_backup_hash
    try:
        h = hashlib.md5(text.encode('utf-8')).hexdigest()
        if h == _last_backup_hash:
            return
        BACKUP_DIR.mkdir(exist_ok=True)
        giorno = datetime.datetime.now().strftime('%Y-%m-%d')
        with open(BACKUP_DIR / f'crm_data_{giorno}.json', 'w', encoding='utf-8') as f:
            f.write(text)
        _last_backup_hash = h
        files = sorted(BACKUP_DIR.glob('crm_data_*.json'))
        for old in files[:-MAX_BACKUPS]:
            try: old.unlink()
            except Exception: pass
    except Exception as e:
        print(f"  (backup automatico non riuscito: {e})")

def save_data(data):
    return crm_db.save_data(data)

try:
    crm_db.seed_from_file_if_empty()
except Exception as _e:
    print("seed:",_e)

@app.route('/')
def index():
    if HTML_FILE.exists():
        return send_file(str(HTML_FILE))
    return "CRM_Arte.html non trovato nella stessa cartella di crm_server.py", 404

@app.route("/api/login", methods=["POST"])
def api_login():
    if not crm_auth.USE_AUTH:
        return jsonify({"ok":True,"ruolo":"titolare","nome":"locale"})
    body=request.get_json(force=True) or {}
    u=crm_auth.controlla_login(body.get("utente",""), body.get("password",""))
    if not u:
        return jsonify({"error":"Utente o password errati."}), 401
    from flask import make_response
    resp=make_response(jsonify({"ok":True,"ruolo":u["ruolo"],"nome":u["nome"]}))
    resp.set_cookie("crm_token", crm_auth.crea_token(u["nome"],u["ruolo"]), httponly=True, samesite="Lax", max_age=12*3600)
    return resp
@app.route("/api/logout", methods=["POST"])
def api_logout():
    from flask import make_response
    resp=make_response(jsonify({"ok":True}))
    resp.set_cookie("crm_token","",max_age=0)
    return resp
@app.route("/api/chisono")
def api_chisono():
    u=_utente_corrente()
    if not u: return jsonify({"login":False})
    return jsonify({"login":True,"nome":u["nome"],"ruolo":u["ruolo"],"online":crm_auth.USE_AUTH})
@app.route('/api/status')
def status():
    data = load_data()
    has_data = bool(data.get('contacts') and len(data['contacts']) > 0)
    return jsonify({'hasData': has_data, 'contacts': len(data.get('contacts', []))})

@app.route('/api/load')
def api_load():
    data = load_data()
    if not data.get('contacts'):
        return jsonify({'error': 'no data'}), 404
    return jsonify(data)

@app.route('/api/save', methods=['POST'])
def api_save():
    try:
        incoming = request.get_json(force=True)
        # Load existing data to preserve opere
        existing = load_data()
        # Update contacts and telefonate (user-editable)
        existing['contacts']  = incoming.get('contacts', existing.get('contacts', []))
        existing['telefonate'] = incoming.get('telefonate', existing.get('telefonate', []))
        existing['lastIdx']   = incoming.get('lastIdx', 0)
        save_data(existing)
        return jsonify({'ok': True, 'contacts': len(existing['contacts'])})
    except Exception as e:
        print(f"Save error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/save_full', methods=['POST'])
def api_save_full():
    """Called on first load to save everything. Preserves verifiche (and any
    other keys already on disk) so a full re-save never wipes the queue."""
    try:
        incoming = request.get_json(force=True)
        existing = load_data() or {}
        existing['contacts']   = incoming.get('contacts', existing.get('contacts', []))
        existing['telefonate'] = incoming.get('telefonate', existing.get('telefonate', []))
        existing['opere']      = incoming.get('opere', existing.get('opere', []))
        existing['lastIdx']    = incoming.get('lastIdx', existing.get('lastIdx', 0))
        # verifiche: usa quelle inviate se presenti, altrimenti conserva quelle su disco
        if 'verifiche' in incoming:
            existing['verifiche'] = incoming['verifiche']
        # ogni altra chiave già su disco resta invariata
        save_data(existing)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/reset', methods=['POST'])
@solo_titolare('reset')
def api_reset():
    save_data({})
    return jsonify({'ok': True})

@app.route('/api/export')
@solo_titolare('export_massa')
def api_export():
    data = load_data()
    contacts = data.get('contacts', [])
    fields = ['ID_contatto','Cognome','Nome','Titolo','Cellulare','Telefono',
              'Citta','Provincia','CAP','Regione','Via','Codice_fiscale',
              'Data_nascita','Note','Esito_ultima_chiamata','Prossima_telefonata','Ora_appuntamento']
    lines = [','.join(fields)]
    for c in contacts:
        row = []
        for f in fields:
            v = str(c.get(f,'') or '')
            if ',' in v or '"' in v or '\n' in v:
                v = '"' + v.replace('"','""') + '"'
            row.append(v)
        lines.append(','.join(row))
    csv_text = '\ufeff' + '\n'.join(lines)
    from flask import Response
    return Response(csv_text, mimetype='text/csv',
        headers={'Content-Disposition':'attachment;filename=contatti_backup.csv'})


@app.route('/api/pdf', methods=['POST'])
def api_pdf():
    """Genera il PDF (layout scheda) per gli ID richiesti. Body: {ids:[...]}.
    Ritorna il PDF pronto da scaricare/allegare. Funziona uguale online."""
    try:
        import crm_pdf
    except Exception as e:
        return jsonify({'error': 'modulo PDF non disponibile: ' + str(e),
                        'hint': 'manca reportlab: lo installa AVVIA_CRM.command'}), 500
    try:
        body = request.get_json(force=True) or {}
        ids = [str(x) for x in body.get('ids', []) if str(x).strip()]
        if not ids:
            return jsonify({'error': 'nessun contatto da stampare'}), 400
        if len(ids) > 3000:
            ids = ids[:3000]
        data = load_data()
        by = {str(c.get('ID_contatto')): c for c in data.get('contacts', [])}
        contatti = [by[i] for i in ids if i in by]
        if not contatti:
            return jsonify({'error': 'contatti non trovati'}), 404
        opere_by = {}
        for o in data.get('opere', []):
            opere_by.setdefault(str(o.get('ID_contatto')), []).append(o)
        tel_by = {}
        for t in data.get('telefonate', []):
            tel_by.setdefault(str(t.get('ID_contatto')), []).append(t)
        pdf = crm_pdf.genera_pdf(contatti, opere_by, tel_by)
        from flask import Response
        if len(contatti) == 1:
            c = contatti[0]
            nome = ((c.get('Cognome') or '') + '_' + (c.get('Nome') or '')).strip('_')
            nome = re.sub(r'[^A-Za-z0-9_]+', '_', nome) or 'scheda'
            fname = 'Scheda_' + nome + '.pdf'
        else:
            fname = 'Schede_%d_contatti.pdf' % len(contatti)
        return Response(pdf, mimetype='application/pdf',
            headers={'Content-Disposition': 'attachment;filename=' + fname})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ════════════════════════════════════════════════════════════
#  GEOCODIFICA "ZONA DI LAVORO" (su richiesta, gratis, Nominatim)
#  Geocodifica i COMUNI (centri-paese), non ogni indirizzo:
#  poche decine per provincia, salvati in geo_comuni.json.
# ════════════════════════════════════════════════════════════
import urllib.request, urllib.parse, time, math, threading

GEO_FILE = BASE_DIR / 'geo_comuni.json'
_geo_lock = threading.Lock()

def _load_geo():
    if GEO_FILE.exists():
        try:
            with open(GEO_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_geo(g):
    with open(GEO_FILE, 'w', encoding='utf-8') as f:
        json.dump(g, f, ensure_ascii=False, indent=None)

def _geo_key(comune, prov):
    return (str(comune or '').strip().upper() + '|' + str(prov or '').strip().upper())

def geocode_comune(comune, prov):
    """Ritorna (lat, lng) per un comune, usando Nominatim. None se non trovato."""
    q = ', '.join([x for x in [comune, prov, 'Italia'] if x])
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(
        {'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'it'})
    req = urllib.request.Request(url, headers={
        'User-Agent': 'CRM-Collezioni-Istituzionali/1.0 (uso interno gestionale)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
    except Exception as e:
        print(f"  geocode errore per {q}: {e}")
    return None

def haversine_km(a, b):
    R = 6371.0
    dlat = math.radians(b[0]-a[0]); dlng = math.radians(b[1]-a[1])
    s = (math.sin(dlat/2)**2 + math.cos(math.radians(a[0]))*math.cos(math.radians(b[0]))*math.sin(dlng/2)**2)
    return 2*R*math.asin(math.sqrt(s))

@app.route('/api/zona', methods=['POST'])
def api_zona():
    """Restituisce i contatti vicini all'appuntamento entro un raggio (km),
    geocodificando i comuni della provincia solo se non già in cache."""
    try:
        body = request.get_json(force=True)
        center_id = str(body.get('id', ''))
        radius = float(body.get('radius', 10))
        data = load_data()
        contacts = data.get('contacts', [])
        cmap = {str(c.get('ID_contatto')): c for c in contacts}
        center = cmap.get(center_id)
        if not center:
            return jsonify({'error': 'contatto non trovato'}), 404
        prov = (center.get('Provincia') or '').strip().upper()
        center_com = (center.get('Citta') or '').strip().upper()
        if not center_com:
            return jsonify({'error': 'il contatto non ha comune'}), 400

        # candidati: stessa provincia (per non geocodificare l'Italia intera)
        cand = [c for c in contacts if (c.get('Provincia') or '').strip().upper() == prov and (c.get('Citta') or '').strip()]
        # comuni distinti da geocodificare
        comuni = {}
        for c in cand:
            k = _geo_key(c.get('Citta'), prov)
            comuni[k] = (c.get('Citta'), prov)
        comuni[_geo_key(center_com, prov)] = (center.get('Citta'), prov)

        with _geo_lock:
            geo = _load_geo()
            todo = [k for k in comuni if k not in geo]
            for i, k in enumerate(todo):
                com, pr = comuni[k]
                coord = geocode_comune(com, pr)
                geo[k] = {'lat': coord[0], 'lng': coord[1]} if coord else None
                time.sleep(1.1)  # cortesia verso Nominatim: ~1/sec
            if todo:
                _save_geo(geo)

        ck = _geo_key(center_com, prov)
        if not geo.get(ck):
            return jsonify({'error': 'non sono riuscito a localizzare il comune dell appuntamento', 'geocoded': len(todo)}), 200
        cpt = (geo[ck]['lat'], geo[ck]['lng'])

        out = []
        for c in cand:
            if str(c.get('ID_contatto')) == center_id:
                continue
            k = _geo_key(c.get('Citta'), prov)
            g = geo.get(k)
            if not g:
                continue
            d = haversine_km(cpt, (g['lat'], g['lng']))
            if d <= radius:
                out.append({
                    'ID_contatto': c.get('ID_contatto'), 'Cognome': c.get('Cognome',''),
                    'Nome': c.get('Nome',''), 'Citta': c.get('Citta',''),
                    'Cellulare': c.get('Cellulare',''), 'Telefono': c.get('Telefono',''),
                    'Esito_ultima_chiamata': c.get('Esito_ultima_chiamata',''),
                    'km': round(d, 1)})
        out.sort(key=lambda x: x['km'])
        return jsonify({'center': {'Citta': center.get('Citta'), 'Provincia': center.get('Provincia')},
                        'radius': radius, 'count': len(out),
                        'geocoded_now': len(todo), 'results': out})
    except Exception as e:
        print(f"Zona error: {e}")
        return jsonify({'error': str(e)}), 500

def geocode_place(q):
    """Geocodifica un luogo libero (testo) -> (lat,lng) o None."""
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(
        {'q': q + ', Italia', 'format': 'json', 'limit': 1, 'countrycodes': 'it'})
    req = urllib.request.Request(url, headers={
        'User-Agent': 'CRM-Collezioni-Istituzionali/1.0 (uso interno gestionale)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode('utf-8'))
        if d:
            return float(d[0]['lat']), float(d[0]['lon']), d[0].get('display_name','')
    except Exception as e:
        print(f"  geocode_place errore {q}: {e}")
    return None

@app.route('/api/zona_libera', methods=['POST'])
def api_zona_libera():
    """Contatti vicini a un LUOGO digitato (zona libera), entro un raggio (km).
    Geocodifica i comuni man mano (cache condivisa con /api/zona)."""
    try:
        body = request.get_json(force=True)
        place = str(body.get('place', '')).strip()
        radius = float(body.get('radius', 15))
        prov_hint = str(body.get('prov', '')).strip().upper()  # opzionale: limita la provincia
        if not place:
            return jsonify({'error': 'scrivi un luogo'}), 400

        gp = geocode_place(place)
        time.sleep(1.1)
        if not gp:
            return jsonify({'error': f'non trovo il luogo "{place}"'}), 200
        cpt = (gp[0], gp[1])

        data = load_data()
        contacts = data.get('contacts', [])
        # candidati: se è indicata una provincia usala; altrimenti tutti i comuni già in cache
        if prov_hint:
            cand = [c for c in contacts if (c.get('Provincia') or '').strip().upper() == prov_hint and (c.get('Citta') or '').strip()]
        else:
            cand = [c for c in contacts if (c.get('Citta') or '').strip()]

        # comuni distinti
        comuni = {}
        for c in cand:
            pr = (c.get('Provincia') or '').strip().upper()
            comuni[_geo_key(c.get('Citta'), pr)] = (c.get('Citta'), pr)

        with _geo_lock:
            geo = _load_geo()
            # se non c'è hint provincia, NON geocodifico tutti i 8000 comuni d'Italia:
            # uso solo quelli già in cache + (se serve) limito. Con hint, li completo.
            if prov_hint:
                todo = [k for k in comuni if k not in geo]
                MAXTODO = 200
                todo = todo[:MAXTODO]
                for k in todo:
                    com, pr = comuni[k]
                    coord = geocode_comune(com, pr)
                    geo[k] = {'lat': coord[0], 'lng': coord[1]} if coord else None
                    time.sleep(1.1)
                if todo:
                    _save_geo(geo)
            else:
                todo = []

        out = []
        for c in cand:
            pr = (c.get('Provincia') or '').strip().upper()
            g = geo.get(_geo_key(c.get('Citta'), pr))
            if not g:
                continue
            d = haversine_km(cpt, (g['lat'], g['lng']))
            if d <= radius:
                out.append({
                    'ID_contatto': c.get('ID_contatto'), 'Cognome': c.get('Cognome',''),
                    'Nome': c.get('Nome',''), 'Citta': c.get('Citta',''), 'Provincia': c.get('Provincia',''),
                    'Cellulare': c.get('Cellulare',''), 'Telefono': c.get('Telefono',''),
                    'Esito_ultima_chiamata': c.get('Esito_ultima_chiamata',''),
                    'km': round(d, 1)})
        out.sort(key=lambda x: x['km'])
        return jsonify({'place': place, 'display': gp[2], 'radius': radius,
                        'count': len(out), 'geocoded_now': len(todo),
                        'hint': bool(prov_hint), 'results': out})
    except Exception as e:
        print(f"Zona libera error: {e}")
        return jsonify({'error': str(e)}), 500

# ════════════════════════════════════════════════════════════
#  DA CONTROLLARE — coda verifiche (unisci / archivia / elimina)
# ════════════════════════════════════════════════════════════
def _open_verifiche(ver):
    return [v for v in ver if v.get('stato') != 'risolto']

def _purge_ids(ver, removed):
    rem = set(map(str, removed))
    for v in ver:
        if v.get('stato') == 'risolto':
            continue
        sch = [str(x) for x in v.get('schede', []) if str(x) not in rem]
        v['schede'] = sch
        if len(sch) < 2 and (str(v.get('tipo','')).startswith(('cellulare','fisso')) or not sch):
            v['stato'] = 'risolto'

def _delete_contact(data, cid):
    cid = str(cid)
    data['contacts']   = [c for c in data.get('contacts', [])   if str(c.get('ID_contatto')) != cid]
    data['opere']      = [o for o in data.get('opere', [])      if str(o.get('ID_contatto')) != cid]
    data['telefonate'] = [t for t in data.get('telefonate', []) if str(t.get('ID_contatto')) != cid]

def _merge_into(data, master, losers):
    # MERGE_COMPLETO: porta TUTTO sulla scheda che resta (opere, telefonate, note, note appuntamento)
    by = {str(c.get('ID_contatto')): c for c in data.get('contacts', [])}
    m = by.get(str(master))
    if not m:
        return
    NOTE_F=['Note','Note_telefono','Esito_ultima_chiamata','Prossima_telefonata','Ora_appuntamento']
    for L in losers:
        lc = by.get(str(L))
        if not lc:
            continue
        # salva il nominativo della scheda unita dentro le Note (non si perde)
        vecchio=((lc.get('Cognome') or '').strip()+' '+(lc.get('Nome') or '').strip()).strip()
        nuovo_n=((m.get('Cognome') or '').strip()+' '+(m.get('Nome') or '').strip()).strip()
        if vecchio and vecchio.lower()!=nuovo_n.lower():
            etich='[scheda unita: '+vecchio+']'
            if etich.lower() not in (m.get('Note') or '').lower():
                m['Note']=((m.get('Note') or '')+(' ' if (m.get('Note') or '').strip() else '')+etich).strip()
        for k, val in lc.items():
            if k in ('ID_contatto', '_local', '_nomecog'):
                continue
            # riempie i campi vuoti del master con quelli del doppione
            if str(val or '').strip() and not str(m.get(k) or '').strip():
                m[k] = val
        # accoda i campi-nota (non perde nulla): Note, Note_telefono, esiti, appuntamenti
        for nf in NOTE_F:
            ln=(lc.get(nf) or '').strip()
            if ln and ln.lower() not in (m.get(nf) or '').lower():
                m[nf]=((m.get(nf) or '')+(' / ' if (m.get(nf) or '').strip() else '')+ln).strip()
    lset = set(map(str, losers))
    # opere e telefonate passano al master
    for o in data.get('opere', []):
        if str(o.get('ID_contatto')) in lset:
            o['ID_contatto'] = str(master)
    for t in data.get('telefonate', []):
        if str(t.get('ID_contatto')) in lset:
            t['ID_contatto'] = str(master)
    # aggiorno i riferimenti nelle altre verifiche (no casi orfani)
    for v in data.get('verifiche', []):
        sch=v.get('schede')
        if isinstance(sch, list):
            v['schede']=[str(master) if str(s) in lset else str(s) for s in sch]
            # dedup mantenendo ordine
            seen=set(); v['schede']=[x for x in v['schede'] if not (x in seen or seen.add(x))]
    data['contacts'] = [c for c in data.get('contacts', []) if str(c.get('ID_contatto')) not in lset]

@app.route('/api/verifica', methods=['POST'])
def api_verifica():
    try:
        body = request.get_json(force=True)
        vid = body.get('vid'); azione = body.get('azione')
        master = str(body.get('master', '')); target = str(body.get('target', ''))
        data = load_data()
        ver = data.get('verifiche', [])
        v = {x.get('id'): x for x in ver}.get(vid)
        if not v:
            return jsonify({'error': 'caso non trovato'}), 404
        by = {str(c.get('ID_contatto')): c for c in data.get('contacts', [])}
        removed = []
        if azione == 'archivia':
            v['stato'] = 'risolto'
        elif azione == 'elimina':
            if target and target in by:
                _delete_contact(data, target); removed.append(target)
                _purge_ids(ver, [target])
            else:
                return jsonify({'error': 'scheda non trovata'}), 404
        elif azione == 'unisci':
            ids = [str(x) for x in v.get('schede', []) if str(x) in by]
            if master not in by or len(ids) < 2:
                return jsonify({'error': 'unione non valida'}), 400
            losers = [i for i in ids if i != master]
            _merge_into(data, master, losers); removed.extend(losers)
            _purge_ids(ver, losers)
            v['stato'] = 'risolto'
        elif azione == 'esito':
            v['esito'] = body.get('esito', '')
        else:
            return jsonify({'error': 'azione sconosciuta'}), 400
        save_data(data)
        master_obj = None
        if azione == 'unisci':
            master_obj = next((c for c in data.get('contacts', []) if str(c.get('ID_contatto')) == master), None)
        return jsonify({'ok': True, 'removed': removed, 'verifiche': _open_verifiche(ver), 'master': master_obj})
    except Exception as e:
        print(f"Verifica error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/contatto_stato', methods=['POST'])
def api_contatto_stato():
    """Conferma (stato=attivo) o segnala (stato=da_verificare) un contatto."""
    try:
        body = request.get_json(force=True)
        cid = str(body.get('id', '')); stato = body.get('stato', 'attivo')
        data = load_data()
        c = next((x for x in data.get('contacts', []) if str(x.get('ID_contatto')) == cid), None)
        if not c:
            return jsonify({'error': 'contatto non trovato'}), 404
        c['Stato'] = stato
        save_data(data)
        return jsonify({'ok': True, 'id': cid, 'stato': stato})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/segnala', methods=['POST'])
def api_segnala():
    """Aggiunge un contatto alla coda 'Da controllare' e lo mette in stato da_verificare."""
    try:
        body = request.get_json(force=True)
        cid = str(body.get('id', '')); tipo = body.get('tipo', 'segnalato'); nota = body.get('nota', '')
        data = load_data()
        c = next((x for x in data.get('contacts', []) if str(x.get('ID_contatto')) == cid), None)
        if not c:
            return jsonify({'error': 'contatto non trovato'}), 404
        ver = data.setdefault('verifiche', [])
        nums = [int(re.sub(r'\D', '', v.get('id', '0')) or 0) for v in ver]
        nid = 'V%04d' % ((max(nums) if nums else 0) + 1)
        ver.append({'id': nid, 'tipo': tipo, 'numero': c.get('Cellulare') or c.get('Telefono') or '',
                    'schede': [cid], 'stato': 'aperto', 'nota': nota or 'segnalato manualmente'})
        c['Stato'] = 'da_verificare'
        save_data(data)
        return jsonify({'ok': True, 'verifiche': _open_verifiche(ver)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = 8080
    # backup automatico all'avvio (copia di sicurezza dei dati attuali)
    try:
        if DATA_FILE.exists():
            _auto_backup(DATA_FILE.read_text(encoding='utf-8'))
            nb = len(list(BACKUP_DIR.glob('crm_data_*.json'))) if BACKUP_DIR.exists() else 0
            print(f"  Backup automatico attivo (una copia al giorno in 'backup/', ultimi {MAX_BACKUPS} giorni).")
    except Exception:
        pass
    print(f"""
╔════════════════════════════════════════╗
║         Arte Editoria Monete           ║
╠════════════════════════════════════════╣
║  Apri nel browser:                     ║
║  http://localhost:{port}                  ║
║                                        ║
║  Dati salvati in:                      ║
║  {str(DATA_FILE)[:38]}  ║
║                                        ║
║  Per chiudere: premi Ctrl+C            ║
╚════════════════════════════════════════╝
""")
    # DIAGNOSTICA_AVVIO: mostra da dove legge e cosa carica
    try:
        _d=load_data()
        _nc=len(_d.get('contacts',[])); _nv=len(_d.get('verifiche',[]))
        print('  ----------------------------------------')
        print('  CARTELLA IN USO :', str(BASE_DIR))
        print('  FILE DATI       :', str(DATA_FILE))
        print('  CONTATTI        :', _nc)
        print('  CASI DA CONTROLLARE (coda):', _nv)
        if _nv==0: print('  ATTENZIONE: la coda e vuota in questo file!')
        print('  ----------------------------------------')
    except Exception as _e:
        print('  (diagnostica avvio non riuscita:', _e, ')')
    app.run(host='127.0.0.1', port=port, debug=False)
