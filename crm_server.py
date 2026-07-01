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
def richiede_login(f):
    @wraps(f)
    def w(*a, **k):
        if crm_auth.USE_AUTH and not _utente_corrente():
            return jsonify({"error":"Devi prima accedere."}), 401
        return f(*a, **k)
    return w


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

@app.route('/manifest.json')
def _pwa_manifest():
    return send_from_directory(str(BASE_DIR), 'manifest.json', mimetype='application/json')
@app.route('/sw.js')
def _pwa_sw():
    return send_from_directory(str(BASE_DIR), 'sw.js', mimetype='application/javascript')
@app.route('/<path:_fname>')
def _pwa_static(_fname):
    import os as _os  # STATIC_PWA: servo icone e simili
    if _fname.endswith(('.png','.ico','.json','.js')) and _os.path.exists(str(BASE_DIR/_fname)):
        return send_from_directory(str(BASE_DIR), _fname)
    from flask import abort; abort(404)

@app.route("/api/login", methods=["POST"])
def api_login():
    if not crm_auth.USE_AUTH:
        return jsonify({"ok":True,"ruolo":"titolare","nome":"locale"})
    body=request.get_json(force=True) or {}
    u=crm_auth.controlla_login(body.get("utente",""), body.get("password",""))
    if not u:
        return jsonify({"error":"Utente o password errati."}), 401
    # registro l'accesso (chi, quando) - non deve mai bloccare il login
    try:
        from datetime import datetime as _dt
        _d = load_data() or {}
        _d.setdefault('accessi', [])
        _d['accessi'].insert(0, {'nome': u['nome'], 'ruolo': u['ruolo'], 'zona': u.get('zona',''), 'ts': _dt.now().isoformat(timespec='seconds')})
        if len(_d['accessi']) > 3000:
            _d['accessi'] = _d['accessi'][:3000]
        save_data(_d)
    except Exception as _e:
        pass
    from flask import make_response
    resp=make_response(jsonify({"ok":True,"ruolo":u["ruolo"],"nome":u["nome"],"zona":u.get("zona","")}))
    resp.set_cookie("crm_token", crm_auth.crea_token(u["nome"],u["ruolo"],zona=u.get("zona","")), httponly=True, samesite="Lax", max_age=12*3600)
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
    if not u: return jsonify({"login":False,"online":crm_auth.USE_AUTH})
    zona=u.get("zona","")
    regioni=crm_auth.regioni_della_zona(zona) if zona else None
    return jsonify({"login":True,"nome":u["nome"],"ruolo":u["ruolo"],"online":crm_auth.USE_AUTH,"zona":zona,"regioni":regioni})

def _solo_titolare_api():
    u=_utente_corrente()
    if crm_auth.USE_AUTH and (not u or u.get('ruolo')!='titolare'):
        return False
    return True

@app.route('/api/backup_lista')
@richiede_login
def api_backup_lista():
    if not _solo_titolare_api():
        return jsonify({'error':'riservato al titolare'}),403
    try:
        return jsonify({'backup': crm_db.lista_backup()})
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.route('/api/backup_scarica')
@richiede_login
def api_backup_scarica():
    if not _solo_titolare_api():
        return jsonify({'error':'riservato al titolare'}),403
    giorno=request.args.get('giorno','')
    try:
        dati=crm_db.carica_backup(giorno)
        if dati is None:
            return jsonify({'error':'backup non trovato'}),404
        import json as _json
        from flask import Response
        testo=_json.dumps(dati,ensure_ascii=False)
        return Response(testo, mimetype='application/json',
            headers={'Content-Disposition':f'attachment;filename=crm_backup_{giorno}.json'})
    except Exception as e:
        return jsonify({'error':str(e)}),500


@app.route('/api/diag_utenti')
def api_diag_utenti():
    import os as _os
    raw = _os.environ.get('CRM_UTENTI','')
    info = {
        'variabile_presente': bool(raw),
        'lunghezza_variabile': len(raw),
        'utenti_caricati': sorted(list(crm_auth.UTENTI.keys())),
        'numero_utenti': len(crm_auth.UTENTI),
        'secret_presente': bool(_os.environ.get('CRM_SECRET','')),
        'auth_attiva': crm_auth.USE_AUTH,
    }
    # dettaglio ruoli/zone (senza password)
    info['dettaglio'] = {k: {'ruolo': v.get('ruolo'), 'zona': v.get('zona','')} for k,v in crm_auth.UTENTI.items()}
    return jsonify(info)

@app.route('/api/status')
def status():
    data = load_data()
    has_data = bool(data.get('contacts') and len(data['contacts']) > 0)
    return jsonify({'hasData': has_data, 'contacts': len(data.get('contacts', []))})

def _filtra_per_zona(data, regioni):
    if not regioni:
        return data
    regset=set(r.strip().lower() for r in regioni)
    contatti=[c for c in data.get('contacts',[]) if (c.get('Regione') or '').strip().lower() in regset]
    ids=set(str(c.get('ID_contatto')) for c in contatti)
    opere=[o for o in data.get('opere',[]) if str(o.get('ID_contatto')) in ids]
    tel=[t for t in data.get('telefonate',[]) if str(t.get('ID_contatto')) in ids]
    out=dict(data); out['contacts']=contatti; out['opere']=opere; out['telefonate']=tel
    return out
def _merge_zona(existing, incoming, regioni):
    regset=set(r.strip().lower() for r in regioni)
    fuori=[c for c in existing.get('contacts',[]) if (c.get('Regione') or '').strip().lower() not in regset]
    in_zona=[c for c in incoming.get('contacts',[]) if (c.get('Regione') or '').strip().lower() in regset]
    merged_contacts=fuori+in_zona
    ids_zona=set(str(c.get('ID_contatto')) for c in in_zona)
    ids_fuori=set(str(c.get('ID_contatto')) for c in fuori)
    tel_fuori=[t for t in existing.get('telefonate',[]) if str(t.get('ID_contatto')) in ids_fuori]
    tel_in=[t for t in incoming.get('telefonate',[]) if str(t.get('ID_contatto')) in ids_zona]
    return merged_contacts, tel_fuori+tel_in
def _regioni_utente():
    u=_utente_corrente()
    if u and u.get('zona'):
        return crm_auth.regioni_della_zona(u.get('zona'))
    return None
@app.route('/api/load')
@richiede_login
def api_load():
    data = load_data()
    if not data.get('contacts'):
        return jsonify({'error': 'no data'}), 404
    _reg=_regioni_utente()
    if _reg:
        data=_filtra_per_zona(data,_reg)
    return jsonify(data)

@app.route('/api/save', methods=['POST'])
@richiede_login
def api_save():
    try:
        incoming = request.get_json(force=True)
        # Load existing data to preserve opere
        existing = load_data()
        # Update contacts and telefonate (user-editable)
        _reg=_regioni_utente()
        if _reg:
            existing['contacts'], existing['telefonate'] = _merge_zona(existing, incoming, _reg)
        else:
            existing['contacts']  = incoming.get('contacts', existing.get('contacts', []))
            existing['telefonate'] = incoming.get('telefonate', existing.get('telefonate', []))
        existing['lastIdx']   = incoming.get('lastIdx', 0)
        save_data(existing)
        return jsonify({'ok': True, 'contacts': len(existing['contacts'])})
    except Exception as e:
        print(f"Save error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/save_full', methods=['POST'])
@richiede_login
def api_save_full():
    """Called on first load to save everything. Preserves verifiche (and any
    other keys already on disk) so a full re-save never wipes the queue."""
    try:
        incoming = request.get_json(force=True)
        existing = load_data() or {}
        _reg=_regioni_utente()
        if _reg:
            existing['contacts'], existing['telefonate'] = _merge_zona(existing, incoming, _reg)
        else:
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


@app.route('/api/aggiungi_telefonata', methods=['POST'])
@richiede_login
def api_aggiungi_telefonata():
    """Salvataggio INCREMENTALE di una singola telefonata/appuntamento.
    Il cellulare invia solo il nuovo dato (payload minimo) invece di tutto
    l'archivio: molto piu' affidabile su connessioni mobili. Il server carica
    i dati, aggiunge la telefonata e aggiorna i campi esito del contatto."""
    try:
        body = request.get_json(force=True)
        tel = body.get('telefonata')
        cid = str(body.get('ID_contatto', '')).strip()
        upd = body.get('contatto_update', {}) or {}
        if not tel or not cid:
            return jsonify({'error': 'dati mancanti'}), 400
        data = load_data() or {}
        data.setdefault('telefonate', [])
        data.setdefault('contacts', [])
        # aggiungo la telefonata in testa
        data['telefonate'].insert(0, tel)
        # aggiorno i campi esito del contatto (solo questi, niente altro)
        campi_ok = {'Esito_ultima_chiamata', 'Prossima_telefonata', 'Ora_appuntamento'}
        for c in data['contacts']:
            if str(c.get('ID_contatto')) == cid:
                for k, v in upd.items():
                    if k in campi_ok:
                        c[k] = v
                break
        save_data(data)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/accessi')
@solo_titolare('accessi')
def api_accessi():
    """Restituisce lo storico accessi (solo titolare)."""
    try:
        d = load_data() or {}
        return jsonify({'accessi': d.get('accessi', [])})
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


# === IMPORT CONTATTI NUOVI (solo titolare) — processo permanente, anti-duplicati ===
def _norm_imp(x):
    import re as _re
    x=str(x or '').upper().strip()
    x=_re.sub(r'\b(ING|ARCH|DOTT|DR|PROF|AVV|GEOM|RAG|SIG|SIGRA|CAV|GEN|COMM|ON|NOT)\.?\b','',x)
    x=_re.sub(r'[^A-Z0-9 ]','',x); x=_re.sub(r'\s+',' ',x).strip(); return x
def _normcf_imp(x):
    x=str(x or '').upper().replace(' ','').strip(); return x if len(x)==16 else ''
def _normtel_imp(x):
    import re as _re
    t=_re.sub(r'[^0-9]','',str(x or ''))
    if t.startswith('39') and len(t)>10: t=t[2:]
    return t if len(t)>=6 else ''
_PROV_REG_IMP={
 'TO':'Piemonte','VC':'Piemonte','NO':'Piemonte','CN':'Piemonte','AT':'Piemonte','AL':'Piemonte','BI':'Piemonte','VB':'Piemonte',
 'AO':"Valle d'Aosta",'GE':'Liguria','SP':'Liguria','SV':'Liguria','IM':'Liguria',
 'MI':'Lombardia','BG':'Lombardia','BS':'Lombardia','CO':'Lombardia','CR':'Lombardia','MN':'Lombardia','PV':'Lombardia','SO':'Lombardia','VA':'Lombardia','LC':'Lombardia','LO':'Lombardia','MB':'Lombardia',
 'VR':'Veneto','VI':'Veneto','PD':'Veneto','VE':'Veneto','TV':'Veneto','RO':'Veneto','BL':'Veneto',
 'UD':'Friuli-Venezia Giulia','PN':'Friuli-Venezia Giulia','GO':'Friuli-Venezia Giulia','TS':'Friuli-Venezia Giulia',
 'TN':'Trentino-Alto Adige','BZ':'Trentino-Alto Adige',
 'RM':'Lazio','VT':'Lazio','RI':'Lazio','LT':'Lazio','FR':'Lazio',
 'PG':'Umbria','TR':'Umbria','AN':'Marche','MC':'Marche','AP':'Marche','PU':'Marche','FM':'Marche',
 'CA':'Sardegna','SS':'Sardegna','NU':'Sardegna','OR':'Sardegna','SU':'Sardegna',
}
def _regione_da_prov_imp(p):
    return _PROV_REG_IMP.get(str(p or '').upper().strip(),'')


def _is_deceduto_imp(c):
    """Riconosce un contatto deceduto dalla parola DECEDUTO/A nelle note,
    escludendo i casi in cui a essere deceduto e' un parente (moglie/marito/figlio...)."""
    import re as _re
    n=str(c.get('Note') or '').upper()
    if 'DECEDUT' not in n: return False
    for m in _re.finditer(r'DECEDUT[OA]', n):
        pre=n[max(0,m.start()-32):m.start()]
        if not _re.search(r"(MOGLIE|MARITO|FIGLI[OA]|MADRE|PADRE|SORELLA|FRATELLO|BABBO|MAMMA|SUOCER[AO]|COMPAGN[OA]|NONN[IOA]|ZI[OA])[\s,:.\']*$", pre):
            return True
    return False

def _norm_nome_imp(c):
    import re as _re
    s=(str(c.get('Cognome') or '')+' '+str(c.get('Nome') or '')+' '+str(c.get('Citta') or '')).upper()
    s=_re.sub(r'[^A-Z0-9 ]','',s); s=_re.sub(r'\s+',' ',s).strip()
    return s

@app.route('/api/importa', methods=['POST'])
@solo_titolare('importa')
def api_importa():
    """Importa contatti nuovi nel database, scartando i duplicati (CF o telefono).
    Body: {contatti:[...], opere:[...], conferma:bool}. Se conferma=False ritorna solo l'anteprima."""
    try:
        payload = request.get_json(force=True)
        nuovi = payload.get('contatti', [])
        opere_in = payload.get('opere', [])
        conferma = payload.get('conferma', False)
        data = load_data() or {}
        contacts = data.get('contacts', [])
        opere = data.get('opere', [])
        # indici anti-duplicati
        cf_idx=set(); tel_idx=set()
        # indici DECEDUTI (per avvisare se un nome in arrivo e' un deceduto gia' noto)
        dec_cf=set(); dec_tel=set(); dec_nome=set()
        maxid=0
        for c in contacts:
            cf=_normcf_imp(c.get('Codice_fiscale'))
            if cf: cf_idx.add(cf)
            tels_c=[]
            for tf in [c.get('Telefono'),c.get('Telefono2'),c.get('Cellulare'),c.get('Cellulare2'),c.get('Tel_Ufficio')]:
                t=_normtel_imp(tf)
                if t: tel_idx.add(t); tels_c.append(t)
            if _is_deceduto_imp(c):
                if cf: dec_cf.add(cf)
                for t in tels_c: dec_tel.add(t)
                nn=_norm_nome_imp(c)
                if nn: dec_nome.add(nn)
            try: maxid=max(maxid,int(c.get('ID_contatto') or 0))
            except: pass
        # filtro i nuovi
        da_inserire=[]; scartati=0; deceduti=[]
        mappa_id={}  # id provvisorio -> id reale
        for c in nuovi:
            cf=_normcf_imp(c.get('Codice_fiscale'))
            tels=[_normtel_imp(c.get('Telefono')),_normtel_imp(c.get('Cellulare'))]
            nn=_norm_nome_imp(c)
            # controllo DECEDUTO: per CF, telefono o cognome+citta
            e_deceduto = (cf and cf in dec_cf) or any(t and t in dec_tel for t in tels) or (nn and nn in dec_nome)
            if e_deceduto:
                deceduti.append((str(c.get('Cognome') or '')+' '+str(c.get('Nome') or '')).strip()+' ('+str(c.get('Citta') or '')+')')
                scartati+=1; continue
            if cf and cf in cf_idx: scartati+=1; continue
            if any(t and t in tel_idx for t in tels): scartati+=1; continue
            da_inserire.append(c)
            # aggiorno gli indici per evitare duplicati DENTRO lo stesso file
            if cf: cf_idx.add(cf)
            for t in tels:
                if t: tel_idx.add(t)
        # anteprima
        if not conferma:
            return jsonify({'anteprima':True,'nuovi':len(da_inserire),'duplicati':scartati,
                            'deceduti':len(deceduti),'deceduti_lista':deceduti[:50],'totale':len(nuovi)})
        # CONFERMA: inserisco davvero
        nid=maxid+1
        campi=set()
        for c in contacts[:50]: campi.update(c.keys())
        for c in da_inserire:
            old=c.get('ID_contatto')
            c['ID_contatto']=str(nid)
            if old is not None: mappa_id[str(old)]=str(nid)
            if not c.get('Regione'):
                c['Regione']=_regione_da_prov_imp(c.get('Provincia'))
            for k in campi:
                if k not in c: c[k]=''
            contacts.append(c); nid+=1
        # opere collegate (rimappo gli ID)
        n_op=0
        for o in opere_in:
            oid=str(o.get('ID_contatto'))
            if oid in mappa_id:
                o['ID_contatto']=mappa_id[oid]
                opere.append(o); n_op+=1
        data['contacts']=contacts; data['opere']=opere
        save_data(data)
        return jsonify({'ok':True,'inseriti':len(da_inserire),'scartati':scartati,'deceduti':len(deceduti),'opere':n_op,'totale_db':len(contacts)})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error':str(e)}), 500

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
