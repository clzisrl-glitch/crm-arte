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

def _ricarica_utenti():
    """Rilegge dal database gli utenti gestiti dentro il CRM (password
    cifrate) e li passa a crm_auth. Chiamata all'avvio e dopo ogni modifica."""
    try:
        n = crm_auth.imposta_utenti_db(crm_db.utenti_lista())
        return n
    except Exception as e:
        print(f"  (utenti dal database non letti: {e})")
        return 0
try:
    _n_utenti = _ricarica_utenti()
    if _n_utenti:
        print(f"  Utenti gestiti dal CRM (password cifrate): {_n_utenti}")
except Exception:
    pass

# ---------------------------------------------------------------------------
# SERIALIZZAZIONE DELLE SCRITTURE
# Ogni rotta che scrive fa: load_data() -> modifica -> save_data().
# Se due operatori salvano nello stesso momento leggono entrambi la stessa
# versione e il secondo che scrive CANCELLA le modifiche del primo, senza
# nessun errore visibile (entrambi vedono "Salvato").
# Con l'archivio attuale la finestra e' di alcuni secondi, quindi il rischio
# e' concreto. Qui le richieste di scrittura vengono messe in fila.
# ---------------------------------------------------------------------------
ROTTE_CHE_SCRIVONO = {
    '/api/salva_contatto', '/api/salva_scheda', '/api/elimina_contatto',
    '/api/aggiungi_telefonata', '/api/contatto_stato', '/api/segnala',
    '/api/verifica', '/api/save', '/api/save_full', '/api/importa',
    '/api/correggi_dati', '/api/fondi_duplicati', '/api/reset', '/api/ordine_schede',
    '/api/bulk_assegna_orari', '/api/copia_ripristina', '/api/elimina_contatti',
    # 12/09/2026: /api/login scrive il registro accessi dentro l'archivio, quindi
    # deve prendere il blocco come tutte le altre. Senza, un accesso fatto mentre
    # una telefonista salva una scheda riscriveva la versione letta PRIMA di quel
    # salvataggio e lo cancellava in silenzio.
    '/api/login',
}

# ---------------------------------------------------------------------------
# DURATA DELLA SESSIONE - NESSUNA (25/08/2026)
# Storia del bug "CRM non salva" (vedi rapporto, appendice 3): dall'11/08 il
# token scadeva 2 ore ESATTE dopo il login, senza rinnovo, e chi lavorava
# piu' a lungo (normale per un CRM usato tutto il giorno) vedeva ogni
# salvataggio rifiutato con 401 "Devi prima accedere". Corretto prima con
# una scadenza "a scorrimento" (12 ore, poi 30 giorni, rinnovata a ogni
# richiesta) - ma restava un limite, per quanto remoto, e il titolare ha
# chiesto esplicitamente di toglierlo del tutto: "MA RESTANO LE ORE".
# Tolto: crm_auth.verifica_token non controlla piu' nessuna scadenza (vedi
# crm_auth.py). Un token con firma valida resta valido finche' esiste il
# cookie che lo contiene. La sicurezza non sparisce, cambia solo dove sta:
#   - il cookie e' "di sessione" (nessun max_age, sotto in /api/login):
#     sparisce da solo alla chiusura del browser, quindi riaprendolo va
#     comunque rifatto il login;
#   - il pulsante Esci (/api/logout) resta un logout esplicito immediato;
#   - in emergenza, cambiare CRM_SECRET su Railway invalida tutti i login
#     in un colpo solo (sezione 7 del rapporto).
#
# AGGIORNATO IL 24/09/2026 — questo restava vero per la DURATA della
# sessione (nessun limite di ore complessive), ma il titolare ha chiesto un
# limite separato sull'INATTIVITA', solo per gli operatori: «SOLO PER
# OPERATORI DOPO 1 ORA DI INATTIVITA DEVONO RIFARE IL LOGIN». Il rinnovo
# torna, ma mirato: dopo ogni richiesta autenticata di un operatore (tranne
# il solo controllo automatico di sfondo, vedi ROTTE_SENZA_ATTIVITA sotto)
# il cookie viene riscritto con un "attivo" fresco (crm_auth.crea_token,
# stesso "creato" originale). crm_auth.verifica_token rifiuta il token se
# sono passati piu' di SESSIONE_INATTIVITA_SEC dall'ultimo "attivo". Il
# titolare non è toccato: per lui verifica_token non controlla ne' il
# giorno ne' l'inattivita', quindi il rinnovo qui sotto è no-op per lui
# (comunque saltato: si applica solo se ruolo != titolare).
# ---------------------------------------------------------------------------

# Rotte del controllo automatico di sfondo (ogni 2 minuti, anche a browser
# abbandonato — vedi CRM_Arte.html, _controllaAggiornamenti): NON contano
# come attivita' vera, altrimenti l'inattivita' di un operatore non
# scadrebbe mai finche' la scheda resta aperta in un'altra finestra.
# /api/logout ci sta per un motivo diverso e piu' importante: la richiesta
# che arriva a /api/logout porta ancora il VECCHIO cookie (il browser lo ha
# gia' mandato prima che il server lo cancelli), quindi senza questa
# esclusione il rinnovo riscriverebbe un cookie valido appena dopo che
# l'operatore ha premuto Esci.
ROTTE_SENZA_ATTIVITA = {'/api/status', '/api/chisono', '/api/logout'}

@app.before_request
def _prendi_blocco():
    from flask import g, request as _rq
    g._blocco = None
    if _rq.method == 'POST' and _rq.path in ROTTE_CHE_SCRIVONO:
        try:
            cm = crm_db.blocco_scrittura()
            cm.__enter__()
            g._blocco = cm
        except Exception as e:
            print(f"blocco scrittura non disponibile: {e}")

@app.teardown_request
def _rilascia_blocco(exc=None):
    from flask import g
    cm = getattr(g, '_blocco', None)
    if cm is not None:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass

from functools import wraps
PORT = int(os.environ.get("PORT","8080"))
def _utente_corrente():
    if not crm_auth.USE_AUTH:
        return {"nome":"locale","ruolo":"titolare"}
    u = crm_auth.verifica_token(request.cookies.get("crm_token",""))
    if not u:
        return None
    # Ruolo e zona si rileggono a ogni richiesta dall'elenco utenti, non dal
    # cookie. Se l'utenza non si trova puo' essere stata creata dall'altro
    # worker: si ricarica l'elenco e si riprova UNA volta, altrimenti si
    # butterebbe fuori un utente legittimo.
    agg = crm_auth.aggiorna_da_elenco(u)
    if agg is None:
        try:
            _ricarica_utenti()
        except Exception:
            return u          # elenco non leggibile: non blocco il lavoro
        agg = crm_auth.aggiorna_da_elenco(u)
    return agg
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
        if crm_auth.USE_AUTH:
            u = _utente_corrente()
            if not u:
                return jsonify({"error":"Devi prima accedere."}), 401
            # Orario di lavoro: le telefoniste entrano solo nella fascia
            # prevista (ore italiane). Il titolare non ha limiti.
            if crm_auth.fuori_orario(u["ruolo"]):
                return jsonify({"error": crm_auth.messaggio_fuori_orario(),
                                "fuori_orario": True}), 403
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

def save_data(data, forza=False, conferma_riduzione=False, copia_prima=''):
    """L'autore lo ricava da solo dalla sessione: cosi' ogni scrittura, anche
    quelle scritte mesi fa, finisce firmata senza doverle toccare una per una."""
    autore = ''
    try:
        u = _utente_corrente()
        autore = (u or {}).get('nome', '') or ''
    except Exception:
        pass
    return crm_db.save_data(data, forza=forza, autore=autore,
                            conferma_riduzione=conferma_riduzione,
                            copia_prima=copia_prima)
def blocco_scrittura():
    return crm_db.blocco_scrittura()

try:
    crm_db.seed_from_file_if_empty()
except Exception as _e:
    print("seed:",_e)

@app.route('/')
def index():
    if HTML_FILE.exists():
        from flask import make_response
        resp = make_response(send_file(str(HTML_FILE)))
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        return resp
    return "CRM_Arte.html non trovato nella stessa cartella di crm_server.py", 404

@app.route('/manifest.json')
def _pwa_manifest():
    return send_from_directory(str(BASE_DIR), 'manifest.json', mimetype='application/json')
@app.route('/sw.js')
def _pwa_sw():
    from flask import make_response
    resp = make_response(send_from_directory(str(BASE_DIR), 'sw.js', mimetype='application/javascript'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp
# File pubblici: SOLO quelli che servono prima del login (icone e PWA).
# 11/09/2026: prima questa rotta serviva QUALSIASI .json della cartella senza
# chiedere l'accesso. Erano quindi scaricabili da chiunque:
#   correzioni.json  -> note commerciali sui clienti, con ID contatto
#   fondi.json       -> telefoni, cellulari, note, valore acquistato
#   coordinate.json  -> le coordinate di casa di ~88.000 persone, per ID
# Ora tutto il resto passa dal login.
PUBBLICI = {'manifest.json', 'sw.js', 'icona-192.png', 'icona-512.png'}
VIETATI = {'correzioni.json', 'fondi.json'}   # servono solo al server, mai al browser

@app.route('/<path:_fname>')
def _pwa_static(_fname):
    import os as _os  # STATIC_PWA: servo icone e simili
    nome = _fname.strip().lstrip('./')
    if nome in VIETATI:
        from flask import abort; abort(404)
    if not _os.path.exists(str(BASE_DIR/nome)):
        from flask import abort; abort(404)
    if nome in PUBBLICI or nome.endswith(('.png', '.ico')):
        return send_from_directory(str(BASE_DIR), nome)
    if nome.endswith(('.json', '.js')):
        _u = _utente_corrente() if crm_auth.USE_AUTH else None
        if crm_auth.USE_AUTH and not _u:
            return jsonify({"error": "Devi prima accedere."}), 401
        if crm_auth.USE_AUTH and crm_auth.fuori_orario(_u["ruolo"]):
            return jsonify({"error": crm_auth.messaggio_fuori_orario(),
                            "fuori_orario": True}), 403
        return send_from_directory(str(BASE_DIR), nome)
    from flask import abort; abort(404)

@app.route("/api/login", methods=["POST"])
def api_login():
    if not crm_auth.USE_AUTH:
        return jsonify({"ok":True,"ruolo":"titolare","nome":"locale"})
    body=request.get_json(force=True) or {}
    _nome_tentato = body.get("utente","")
    # Blocco dopo 3 tentativi sbagliati (5 minuti). Le funzioni esistevano in
    # crm_auth.py dall'inizio ma NON venivano chiamate da nessuno: si potevano
    # provare password all'infinito. Collegate l'11/09/2026.
    _resta = crm_auth.stato_blocco(_nome_tentato)
    if _resta > 0:
        return jsonify({"error": f"Troppi tentativi. Riprova fra {_resta//60+1} minuti."}), 429
    u=crm_auth.controlla_login(_nome_tentato, body.get("password",""))
    crm_auth.registra_tentativo(_nome_tentato, bool(u))
    if not u:
        return jsonify({"error":"Utente o password errati."}), 401
    # Password giusta ma fuori dall'orario di lavoro: niente accesso.
    if crm_auth.fuori_orario(u["ruolo"]):
        return jsonify({"error": crm_auth.messaggio_fuori_orario(),
                        "fuori_orario": True}), 403
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
    # COOKIE DI SESSIONE: niente max_age -> il browser lo cancella alla chiusura,
    # cosi' alla riapertura le credenziali vengono richieste di nuovo. Il
    # token stesso non ha piu' nessuna scadenza a ore (vedi nota sopra e
    # crm_auth.py): resta valido finche' esiste questo cookie.
    resp.set_cookie("crm_token", crm_auth.crea_token(u["nome"],u["ruolo"],zona=u.get("zona","")), httponly=True, samesite="Lax", secure=True)
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
    # Nomi (non le password) degli account titolare: servono al frontend per
    # nascondere dall'agenda degli operatori le telefonate/appuntamenti
    # registrati dal titolare/admin su contatti della loro stessa zona
    # (l'operatore deve vedere SOLO la propria agenda, non quella dell'admin
    # ne' quella di un'altra zona).
    titolari=crm_auth.nomi_titolari()
    return jsonify({"login":True,"nome":u["nome"],"ruolo":u["ruolo"],"online":crm_auth.USE_AUTH,
                    "zona":zona,"regioni":regioni,"titolari":titolari,
                    "ora_italiana":crm_auth._ora_italiana().strftime('%H:%M'),
                    "ora_apertura":crm_auth.ORA_APERTURA,
                    "ora_chiusura":crm_auth.ORA_CHIUSURA})

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


@app.route('/api/copie')
@richiede_login
def api_copie():
    """Elenco di TUTTE le copie: giornaliere, orarie, e quelle fatte prima di
    una riduzione confermata. Con i numeri di ciascuna e la variazione
    rispetto alla precedente, cosi' si sceglie guardando le cifre."""
    if not _solo_titolare_api():
        return jsonify({'error': 'riservato al titolare'}), 403
    try:
        out = {'copie': crm_db.elenco_copie(),
               'soglia': int(round(crm_db.SOGLIA_PERC * 100)),
               'allarme_lettura': getattr(crm_db, 'ALLARME_LETTURA', '')}
        if request.args.get('verifica'):
            # ~10 millesimi per copia: il calcolo lo fa PostgreSQL, i 7 MB
            # di ogni copia non attraversano mai la rete.
            esiti = crm_db.verifica_integrita()
            for r in out['copie']:
                e = esiti.get(r['chiave'])
                if e:
                    r['integrita'] = e['segno']
                    r['integrita_detto'] = e['detto']
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/copia_verifica')
@richiede_login
def api_copia_verifica():
    """LA VERIFICA A FONDO di una copia: la apre davvero e ricontrolla il
    contenuto, non solo i byte. Un paio di secondi, su richiesta."""
    if not _solo_titolare_api():
        return jsonify({'error': 'riservato al titolare'}), 403
    chiave = request.args.get('chiave', '')
    if not chiave:
        return jsonify({'error': 'chiave mancante'}), 400
    try:
        return jsonify(crm_db.verifica_a_fondo(chiave))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/copia_ripristina', methods=['POST'])
@solo_titolare('salva_tutto')
def api_copia_ripristina():
    """Rimette in linea una copia. Prima di farlo:
       1. la copia viene aperta e verificata a fondo (mai ripristinare alla
          cieca: e' il momento in cui una copia sbagliata costa di piu');
       2. lo stato ATTUALE viene messo da parte come copia da evento, cosi'
          anche un ripristino sbagliato si puo' disfare.
    Il ripristino riduce quasi sempre i numeri: passa con conferma_riduzione."""
    try:
        body = request.get_json(force=True) or {}
        chiave = str(body.get('chiave', '')).strip()
        if not chiave:
            return jsonify({'error': 'chiave mancante'}), 400
        esito = crm_db.verifica_a_fondo(chiave)
        if not esito.get('ok'):
            return jsonify({'error': 'copia non utilizzabile: ' + esito.get('detto', ''),
                            'dettaglio': esito.get('dettaglio', {})}), 409
        if esito.get('segno') == '!' and not body.get('conferma_anomalia'):
            return jsonify({'error': 'la copia presenta anomalie: ' + esito.get('detto', ''),
                            'anomalia': True, 'dettaglio': esito.get('dettaglio', {})}), 409
        # Un ripristino torna indietro nel tempo: quasi sempre riporta NUMERI
        # PIU' BASSI di adesso, ed e' normale. Ma se la copia e' molto piu'
        # piccola dell'archivio di oggi, chi ripristina deve saperlo prima,
        # con i numeri sotto gli occhi: cosi' non si scambia una copia rotta
        # per quella buona proprio nel momento peggiore.
        dett = esito.get('dettaglio') or {}
        if not body.get('conferma_riduzione'):
            try:
                adesso = crm_db._checkup(load_data() or {})
                calo = crm_db._peggior_calo(adesso, dett)
                if calo:
                    return jsonify({'error': 'la copia e\' molto piu\' piccola dell\'archivio '
                                             'attuale (' + calo[0] + '). Controlla i numeri '
                                             'e conferma se e\' quella giusta.',
                                    'riduzione': True, 'adesso': adesso,
                                    'dettaglio': dett}), 409
            except Exception:
                pass
        dati = crm_db.carica_backup(chiave)
        if dati is None:
            return jsonify({'error': 'copia non trovata'}), 404
        # il blocco di scrittura lo prende gia' before_request: la rotta e'
        # nell'elenco ROTTE_CHE_SCRIVONO
        save_data(dati, conferma_riduzione=True)
        return jsonify({'ok': True, 'chiave': chiave, 'dettaglio': esito.get('dettaglio', {})})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/diag_utenti')
@solo_titolare('gestione_utenti')
def api_diag_utenti():
    # 11/09/2026: era aperta. Elencava nomi utente, ruoli e zone: meta'
    # delle credenziali servita a chiunque la chiedesse.
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

@app.route("/api/correggi_dati", methods=["POST"])
def api_correggi_dati():
    if not _solo_titolare_api():
        return jsonify({"error":"riservato al titolare"}),403
    import os as _os, json as _json
    p=_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),"correzioni.json")
    try:
        corr=_json.load(open(p,encoding="utf-8"))
    except Exception as e:
        return jsonify({"error":"correzioni.json: "+str(e)}),500
    data=load_data() or {}
    idx={str(c.get("ID_contatto")):c for c in data.get("contacts",[])}
    npv=nrg=nct=nst=ntel=nnt=0
    for i,v in (corr.get("aggiorna") or {}).items():
        c=idx.get(i)
        if not c: continue
        if v.get("Provincia") and c.get("Provincia")!=v["Provincia"]:
            c["Provincia"]=v["Provincia"]; npv+=1
        if v.get("Regione") and c.get("Regione")!=v["Regione"]:
            c["Regione"]=v["Regione"]; nrg+=1
        # Citta: serve alla pulizia frazioni (comune nel campo Citta, frazione
        # conservata). Se indicata, la frazione va in Note per non perderla.
        if v.get("Citta") and c.get("Citta")!=v["Citta"]:
            fz=(v.get("Frazione") or "").strip()
            if fz and fz.lower() not in (c.get("Note") or "").lower():
                c["Note"]=((c.get("Note") or "").strip()+" [fraz. "+fz+"]").strip()
            c["Citta"]=v["Citta"]; nct+=1
        # Stato: per togliere dalla lavorazione i contatti senza recapito.
        # NotaAggiungi si accoda alle Note solo se non c'e' gia', cosi'
        # rilanciare la correzione non duplica il testo.
        if v.get("Stato") and c.get("Stato")!=v["Stato"]:
            c["Stato"]=v["Stato"]; nst+=1
        # TELEFONI: ricostruzione dei numeri incompleti (zero iniziale mancante,
        # prefisso del comune mancante). Solo questi campi, mai altri.
        for _tk in ("Telefono","Telefono2","Tel_Ufficio","Cellulare","Cellulare2"):
            # "in v" e non "v.get(...)": la stringa vuota e' un valore valido e
            # serve a SVUOTARE un campo (es. frammenti tipo "075" o "335").
            # Con v.get() la stringa vuota sarebbe falsa e il campo non verrebbe
            # mai ripulito.
            if _tk in v and str(c.get(_tk) or "")!=str(v[_tk] or ""):
                c[_tk]=v[_tk]; ntel+=1
        na=(v.get("NotaAggiungi") or "").strip()
        if na and na.lower() not in (c.get("Note") or "").lower():
            c["Note"]=((c.get("Note") or "").strip()+" "+na).strip()
        # Note: sostituzione diretta e completa del testo (usata per unificare
        # sigle tipo VIP/TOP/GEO/NOC/TRC in TREC/EDIT). A differenza di
        # NotaAggiungi (che accoda), qui il nuovo testo sostituisce tutto il campo.
        if "Note" in v and str(c.get("Note") or "") != str(v["Note"] or ""):
            c["Note"]=v["Note"]; nnt+=1
    if npv or nrg or nct or nst or ntel or nnt: save_data(data)
    return jsonify({"ok":True,"province":npv,"regioni":nrg,"citta":nct,
                    "stati":nst,"telefoni":ntel,"note":nnt})

@app.route("/api/fondi_duplicati", methods=["POST"])
def api_fondi_duplicati():
    if not _solo_titolare_api():
        return jsonify({"error":"riservato al titolare"}),403
    import os as _os, json as _json
    p=_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),"fondi.json")
    try:
        piano=_json.load(open(p,encoding="utf-8")).get("fondi",[])
    except Exception as e:
        return jsonify({"error":"fondi.json: "+str(e)}),500
    data=load_data() or {}
    idx={str(c.get("ID_contatto")):c for c in data.get("contacts",[])}
    da_eliminare=set(); campi=0
    for e in piano:
        keep=idx.get(str(e.get("keep")))
        if not keep: continue
        for k,v in (e.get("set") or {}).items():
            keep[k]=v; campi+=1
        rem=set(str(x) for x in (e.get("remove") or []))
        kid=keep.get("ID_contatto")
        for o in data.get("opere",[]):
            if str(o.get("ID_contatto")) in rem: o["ID_contatto"]=kid
        for t in data.get("telefonate",[]):
            if str(t.get("ID_contatto")) in rem: t["ID_contatto"]=kid
        da_eliminare |= rem
    # VERIFICHE: vanno riagganciate alla scheda superstite, non lasciate
    # appese a un ID che non esiste piu'. La rotta non lo faceva e dopo una
    # fusione restavano segnalazioni orfane (successo il 10/08/2026).
    # Il rimappaggio usa il piano, quindi funziona anche a fusione gia' fatta.
    _mappa={}
    for e in piano:
        k=str(e.get("keep"))
        for r in (e.get("remove") or []): _mappa[str(r)]=k
    _vivi=set(str(c.get("ID_contatto")) for c in data.get("contacts",[])) - da_eliminare
    nver=0; nchiuse=0
    for v in data.get("verifiche",[]):
        if str(v.get("stato",""))=="risolto": continue
        vecchie=[str(x) for x in (v.get("schede") or [])]
        nuove=[]
        for x in vecchie:
            y=x if x in _vivi else _mappa.get(x)
            if y and y in _vivi and y not in nuove: nuove.append(y)
        if nuove!=vecchie:
            v["schede"]=nuove; nver+=1
            if not nuove:
                v["stato"]="risolto"
                v["nota"]=((v.get("nota") or "")+" [chiusa: schede fuse]").strip()
                nchiuse+=1
    prima=len(data.get("contacts",[]))
    data["contacts"]=[c for c in data.get("contacts",[]) if str(c.get("ID_contatto")) not in da_eliminare]
    eliminati=prima-len(data["contacts"])
    save_data(data)
    return jsonify({"ok":True,"eliminati":eliminati,"campi":campi,
                    "verifiche_riagganciate":nver,"verifiche_chiuse":nchiuse})

@app.route('/api/ordine_schede', methods=['GET','POST'])
@richiede_login
def ordine_schede():
    """Ordine delle schede della barra in alto, deciso dal titolare.
    Sta nel database (non nel browser) cosi' vale su tutti i dispositivi e
    per tutti gli operatori. Solo il titolare puo' modificarlo."""
    if request.method == 'GET':
        data = load_data()
        return jsonify({'ok': True, 'ordine': data.get('ordine_schede') or []})
    u = _utente_corrente() if crm_auth.USE_AUTH else {'ruolo':'titolare'}
    if not u or u.get('ruolo') != 'titolare':
        return jsonify({'error': 'Solo il titolare puo cambiare l ordine.'}), 403
    body = request.get_json(force=True) or {}
    ordine = body.get('ordine')
    if not isinstance(ordine, list) or not all(isinstance(x, str) for x in ordine):
        return jsonify({'error': 'Ordine non valido.'}), 400
    data = load_data()
    data['ordine_schede'] = ordine[:40]
    save_data(data)
    return jsonify({'ok': True, 'ordine': data['ordine_schede']})

# Schede ammesse per "riapri dall'ultima scheda": elenco chiuso, cosi' un
# valore sbagliato o manomesso non puo' finire nel database.
# Stati ammessi per un contatto: elenco chiuso, come SCHEDE_VALIDE.
STATI_VALIDI = {'attivo', 'da_verificare', 'potenziale', 'deceduto', 'fuso', ''}

SCHEDE_VALIDE = {
    'agenda','principale','list','provincia','zonalibera','recupero',
    'controllare','potenziali','nocell','welcome','statistiche','deceduti',
}

@app.route('/api/ui_stato', methods=['GET','POST'])
@richiede_login
def api_ui_stato():
    """Ultima scheda aperta da ciascun utente.

    NON passa dal blob dei dati: sta in una tabella a parte (crm_ui, vedi
    crm_db.py), una riga per utente. Cosi' ricordare la scheda non riscrive
    i ~7MB dell'archivio e non puo' in nessun caso danneggiarlo.
    E' per utente, non per browser: chi si sposta dal computer al telefono
    ritrova la stessa scheda."""
    u = _utente_corrente() or {'nome': 'locale'}
    nome = u.get('nome') or 'locale'
    if request.method == 'GET':
        try:
            return jsonify({'ok': True, 'stato': crm_db.ui_get(nome)})
        except Exception as e:
            return jsonify({'ok': True, 'stato': {}, 'nota': str(e)})
    body = request.get_json(force=True) or {}
    scheda = str(body.get('ultima_scheda') or '').strip().lower()
    if scheda not in SCHEDE_VALIDE:
        return jsonify({'error': 'Scheda non valida.'}), 400
    # Ultimo cliente aperto in Principale: solo cifre/lettere, al massimo 20
    # caratteri, cosi' un valore manomesso non puo' finire nel database.
    contatto = str(body.get('ultimo_contatto') or '').strip()[:20]
    if contatto and not contatto.replace('-', '').replace('_', '').isalnum():
        contatto = ''
    try:
        stato = crm_db.ui_get(nome)
        stato['ultima_scheda'] = scheda
        stato['ultimo_contatto'] = contatto
        crm_db.ui_set(nome, stato)
        return jsonify({'ok': True, 'ultima_scheda': scheda, 'ultimo_contatto': contatto})
    except Exception as e:
        # una preferenza non deve MAI far fallire il lavoro dell'operatore
        return jsonify({'ok': False, 'error': str(e)}), 200

# ═══════════════════════════════════════════════════════════════════
#  REGISTRO ATTIVITA' E PRESENZA
# ═══════════════════════════════════════════════════════════════════
# Un solo punto: dopo ogni richiesta andata a buon fine, se era una
# operazione di scrittura la scrivo nel registro. Cosi' non serve ricordarsi
# di aggiungere la riga in ognuna delle 15 rotte (e quelle di domani sono
# coperte da sole, basta che stiano in ROTTE_CHE_SCRIVONO).
AZIONI_LEGGIBILI = {
    '/api/aggiungi_telefonata': 'telefonata registrata',
    '/api/salva_contatto':      'scheda salvata',
    '/api/salva_scheda':        'scheda salvata',
    '/api/bulk_assegna_orari':  'richiami sistemati',
    '/api/contatto_stato':      'stato cambiato',
    '/api/segnala':             'segnalazione',
    '/api/verifica':            'caso da controllare',
    '/api/elimina_contatto':    'scheda ELIMINATA',
    '/api/save':                'salvataggio in blocco',
    '/api/save_full':           'salvataggio in blocco',
    '/api/importa':             'importazione contatti',
    '/api/correggi_dati':       'correzione province',
    '/api/fondi_duplicati':     'fusione duplicati',
    '/api/reset':               'AZZERAMENTO archivio',
    '/api/ordine_schede':       'ordine schede',
    '/api/copia_ripristina':    'RIPRISTINO da copia',
    '/api/messaggi_elimina':    'messaggio cancellato',
    '/api/utenti':              'gestione utenti',
    '/api/utenti_elimina':      'utente eliminato',
    '/api/login':               'accesso',
    '/api/logout':              'uscita',
}
# Non scrivo la presenza a ogni singola richiesta: basta ogni 2 minuti.
_ultimo_tocco = {}
TOCCO_OGNI_SEC = 120

def _id_dal_corpo():
    try:
        b = request.get_json(silent=True) or {}
        for k in ('id_contatto', 'ID_contatto', 'id'):
            v = b.get(k)
            if v not in (None, ''):
                return str(v)[:40]
    except Exception:
        pass
    return ''

def _dettaglio_dal_corpo(percorso):
    try:
        b = request.get_json(silent=True) or {}
        if percorso == '/api/aggiungi_telefonata':
            t = b.get('telefonata') or {}
            e = str(t.get('Esito') or '').strip()
            d = str(t.get('Data_appuntamento') or '').strip()
            return (e + (' ' + d if d else '')).strip()[:200]
        if percorso == '/api/contatto_stato':
            return str(b.get('stato') or '')[:60]
        if percorso == '/api/segnala':
            return str(b.get('nota') or '')[:200]
    except Exception:
        pass
    return ''

@app.after_request
def _registra_attivita(risposta):
    try:
        if not crm_auth.USE_AUTH:
            return risposta
        percorso = request.path
        if risposta.status_code >= 400:
            return risposta
        u = _utente_corrente()
        if percorso == '/api/login':
            # al login l'utente non e' ancora nel cookie: lo prendo dal corpo
            try:
                nome = str((request.get_json(silent=True) or {}).get('utente') or '')[:80]
            except Exception:
                nome = ''
            if nome:
                crm_db.attivita_registra(nome, '', '', 'accesso')
                crm_db.presenza_tocca(nome, '', '')
            return risposta
        if not u:
            return risposta
        nome = u.get('nome') or ''
        # presenza: al massimo una scrittura ogni 2 minuti per utente
        import time as _t
        ora = _t.time()
        if ora - _ultimo_tocco.get(nome, 0) > TOCCO_OGNI_SEC:
            _ultimo_tocco[nome] = ora
            crm_db.presenza_tocca(nome, u.get('ruolo', ''), u.get('zona', ''))
        # registro: solo le operazioni che cambiano qualcosa
        if request.method == 'POST' and percorso in AZIONI_LEGGIBILI:
            crm_db.attivita_registra(nome, u.get('ruolo', ''), u.get('zona', ''),
                                     AZIONI_LEGGIBILI[percorso],
                                     _id_dal_corpo(), _dettaglio_dal_corpo(percorso))
        # NUOVO 24/09/2026: rinnovo del token per l'operatore (vedi nota sopra
        # "DURATA DELLA SESSIONE"). Il titolare non ha questo limite, quindi
        # per lui non serve rinnovare niente.
        if u.get('ruolo') != 'titolare' and percorso not in ROTTE_SENZA_ATTIVITA:
            nuovo_token = crm_auth.crea_token(u['nome'], u.get('ruolo', ''),
                                               zona=u.get('zona', ''), creato=u.get('creato'))
            risposta.set_cookie("crm_token", nuovo_token, httponly=True,
                                 samesite="Lax", secure=True)
    except Exception:
        pass          # il registro non deve mai disturbare il lavoro
    return risposta

# ═══════════════════════════════════════════════════════════════════
#  NOTIFICHE SUL TELEFONO E SUL COMPUTER (anche col CRM chiuso)
# ═══════════════════════════════════════════════════════════════════
# L'invio della notifica va fatto FUORI dalla risposta: se il servizio di
# Google ci mette 8 secondi, la telefonista non deve aspettare 8 secondi per
# vedere il suo messaggio inviato. Quindi:
#   - gli indirizzi si leggono dal database nel thread della richiesta
#     (la connessione PostgreSQL e' una sola e condivisa: NON va usata da un
#      altro thread, si corromperebbe);
#   - la chiamata HTTP va in un thread a parte, che NON tocca il database;
#   - se un indirizzo risulta scaduto, il thread lo mette in una lista e la
#     pulizia avviene alla notifica successiva, nel thread giusto.
import crm_push
_push_da_pulire = []

def _push_pulisci():
    global _push_da_pulire
    if not _push_da_pulire:
        return
    scaduti, _push_da_pulire = _push_da_pulire, []
    for e in scaduti:
        try:
            crm_db.push_disiscrivi(e)
        except Exception:
            pass

def _push_avvisa(destinatari, titolo='CRM Arte'):
    """Manda la sveglia alle iscrizioni di questi utenti. Non solleva mai."""
    try:
        if not crm_auth.USE_AUTH:
            return
        _push_pulisci()
        priv, pub = crm_db.push_chiavi()
        if not priv:
            return
        indirizzi = []
        for nome in destinatari:
            indirizzi.extend(crm_db.push_indirizzi(nome))
        indirizzi = list(dict.fromkeys(indirizzi))     # senza doppioni
        if not indirizzi:
            return

        def _lavora():
            for e in indirizzi:
                ok, codice, nota = crm_push.invia_sveglia(e, priv, pub)
                if codice in (404, 410):
                    _push_da_pulire.append(e)          # iscrizione morta
        import threading
        threading.Thread(target=_lavora, daemon=True).start()
    except Exception:
        pass

@app.route('/api/push/chiave')
@richiede_login
def api_push_chiave():
    priv, pub = crm_db.push_chiavi()
    st = crm_push.stato()
    return jsonify({'ok': bool(pub), 'pubblica': pub or '',
                    'pronto': st.get('pronto', False), 'motivo': st.get('motivo', '')})

@app.route('/api/push/iscrivi', methods=['POST'])
@richiede_login
def api_push_iscrivi():
    u = _utente_corrente() or {}
    b = request.get_json(force=True) or {}
    endpoint = str(b.get('endpoint') or '').strip()
    if not endpoint.startswith('https://'):
        return jsonify({'error': 'indirizzo non valido'}), 400
    chiavi = b.get('keys') or {}
    ok = crm_db.push_iscrivi(u.get('nome'), endpoint,
                             str(chiavi.get('p256dh') or ''), str(chiavi.get('auth') or ''),
                             str(b.get('dispositivo') or '')[:120])
    return jsonify({'ok': bool(ok)})

@app.route('/api/push/disiscrivi', methods=['POST'])
@richiede_login
def api_push_disiscrivi():
    b = request.get_json(force=True) or {}
    crm_db.push_disiscrivi(str(b.get('endpoint') or '').strip())
    return jsonify({'ok': True})

@app.route('/api/push/prova', methods=['POST'])
@richiede_login
def api_push_prova():
    """Manda una notifica a se stessi: serve a provare che funziona."""
    u = _utente_corrente() or {}
    quanti = len(crm_db.push_indirizzi(u.get('nome')))
    if not quanti:
        return jsonify({'ok': False, 'error': 'Questo dispositivo non e ancora iscritto alle notifiche.'}), 400
    _push_avvisa([u.get('nome')])
    return jsonify({'ok': True, 'dispositivi': quanti})

@app.route('/api/push/stato')
@solo_titolare('gestione_utenti')
def api_push_stato():
    priv, pub = crm_db.push_chiavi(crea_se_manca=False)
    return jsonify({'ok': True, 'libreria': crm_push.stato(),
                    'chiavi_presenti': bool(pub),
                    'iscrizioni': crm_db.push_quanti()})

# ═══════════════════════════════════════════════════════════════════
#  MESSAGGI FRA TELEFONISTA E TITOLARE
# ═══════════════════════════════════════════════════════════════════
# Una conversazione per telefonista. Serve a chiedere al titolare le modifiche
# che lei non puo' fare. Una telefonista vede e scrive SOLO nella propria.
@app.route('/api/messaggi', methods=['GET'])
@richiede_login
def api_messaggi():
    u = _utente_corrente() or {}
    titolare = u.get('ruolo') == 'titolare'
    if request.args.get('conteggio'):
        # Chiamata leggera: serve al pallino dei non letti e all'anteprima
        # nella notifica (il service worker la chiama appena si sveglia).
        # L'anteprima si manda SOLO se chi chiede e' collegato: e' la sessione
        # a decidere, non la notifica. Se il cookie non c'e' piu', la notifica
        # resta generica invece di mostrare il testo a chi passa di la'.
        anteprima = crm_db.messaggio_ultimo_non_letto(titolare, u.get('nome')) \
            if request.args.get('anteprima') else None
        if titolare:
            return jsonify({'ok': True, 'titolare': True,
                            'da_leggere': crm_db.messaggi_da_leggere(True),
                            'anteprima': anteprima})
        return jsonify({'ok': True, 'titolare': False,
                        'da_leggere': crm_db.messaggi_da_leggere(False, u.get('nome')),
                        'anteprima': anteprima})
    if titolare:
        # Su quale scheda sta lavorando ciascuno IN QUESTO MOMENTO: lo sappiamo
        # gia', perche' il CRM ricorda l'ultima scheda aperta per utente
        # (tabella crm_ui). Serve al titolare per andare dove e' lei mentre le
        # risponde, senza chiederglielo.
        aperte = {}
        try:
            for nome, dati in (crm_db.ui_tutti() or {}).items():
                aperte[nome] = {'scheda': dati.get('ultima_scheda', ''),
                                'contatto': dati.get('ultimo_contatto', ''),
                                'aggiornato': dati.get('aggiornato', '')}
        except Exception:
            aperte = {}
        chi = (request.args.get('utente') or '').strip()
        if chi:
            crm_db.messaggi_segna_letti(chi, False)   # ho letto quelli di lei
            return jsonify({'ok': True, 'titolare': True, 'utente': chi,
                            'aperte': aperte,
                            'messaggi': crm_db.messaggi_elenco(chi)})
        return jsonify({'ok': True, 'titolare': True,
                        'da_leggere': crm_db.messaggi_da_leggere(True),
                        'aperte': aperte,
                        'messaggi': crm_db.messaggi_elenco(None, 300)})
    mio = u.get('nome') or ''
    crm_db.messaggi_segna_letti(mio, True)            # ho letto quelli del titolare
    return jsonify({'ok': True, 'titolare': False, 'utente': mio,
                    'messaggi': crm_db.messaggi_elenco(mio)})

@app.route('/api/messaggi_elimina', methods=['POST'])
@richiede_login
def api_messaggi_elimina():
    """Cancella un messaggio, o svuota una conversazione.

    Il titolare cancella quello che vuole. La telefonista solo i propri, e solo
    finche' non sono stati letti: un messaggio gia' letto puo' aver prodotto una
    correzione, e toglierlo lascerebbe il titolare senza il perche'.
    Le cancellazioni restano nel registro attivita'."""
    u = _utente_corrente() or {}
    titolare = u.get('ruolo') == 'titolare'
    body = request.get_json(force=True) or {}

    if body.get('tutto'):
        if not titolare:
            return jsonify({'error': 'riservato al titolare'}), 403
        chi = str(body.get('utente') or '').strip()[:80]
        if not chi:
            return jsonify({'error': 'indica la conversazione da svuotare'}), 400
        quanti = crm_db.messaggi_svuota(chi)
        return jsonify({'ok': True, 'eliminati': quanti})

    try:
        ident = int(body.get('id') or 0)
    except Exception:
        ident = 0
    if not ident:
        return jsonify({'error': 'id mancante'}), 400
    fatto, motivo = crm_db.messaggio_elimina(
        ident, chi=u.get('nome'), solo_propri_non_letti=(not titolare))
    if not fatto:
        return jsonify({'error': motivo or 'non eliminato'}), 403
    return jsonify({'ok': True, 'eliminati': 1})


@app.route('/api/messaggi', methods=['POST'])
@richiede_login
def api_messaggi_scrivi():
    u = _utente_corrente() or {}
    titolare = u.get('ruolo') == 'titolare'
    body = request.get_json(force=True) or {}
    testo = str(body.get('testo') or '').strip()[:2000]
    if not testo:
        return jsonify({'error': 'messaggio vuoto'}), 400
    id_contatto = str(body.get('id_contatto') or '').strip()[:40]
    if titolare:
        # il titolare deve dire a CHI scrive
        chi = str(body.get('utente') or '').strip()[:80]
        if not chi:
            return jsonify({'error': 'manca il destinatario'}), 400
    else:
        chi = u.get('nome') or ''      # la telefonista scrive solo a se stessa
    nuovo = crm_db.messaggio_scrivi(chi, titolare, u.get('nome'), testo, id_contatto)
    if not nuovo:
        return jsonify({'error': 'messaggio non salvato'}), 500
    # notifica a chi deve leggere: se scrive la telefonista avviso i titolari,
    # se scrive il titolare avviso lei. Parte in un thread, non rallenta.
    try:
        destinatari = [chi] if titolare else crm_auth.nomi_titolari()
        _push_avvisa([d for d in destinatari if d and d != u.get('nome')])
    except Exception:
        pass
    return jsonify({'ok': True, 'id': nuovo})

@app.route('/api/attivita')
@solo_titolare('accessi')
def api_attivita():
    """Cosa hanno fatto le telefoniste e quanto sono state collegate.
    Solo titolare."""
    try:
        giorni = int(request.args.get('giorni', 7))
    except Exception:
        giorni = 7
    giorni = min(max(giorni, 1), 60)
    return jsonify({'ok': True, 'giorni': giorni,
                    'sessioni': crm_db.sessioni_elenco(giorni),
                    'attivita': crm_db.attivita_elenco(giorni, 400),
                    'adesso': crm_auth._ora_italiana().isoformat(timespec='seconds')})

@app.route('/api/utenti', methods=['GET'])
@solo_titolare('gestione_utenti')
def api_utenti_elenco():
    """Elenco utenti: nome, ruolo, zona e da dove arrivano. MAI le password
    (di quelle gestite dal CRM esiste solo l'impronta, e non esce da qui)."""
    _ricarica_utenti()
    return jsonify({'ok': True,
                    'utenti': crm_auth.elenco_utenti_visibile(),
                    'zone': sorted(crm_auth.ZONE.keys())})

@app.route('/api/utenti', methods=['POST'])
@solo_titolare('gestione_utenti')
def api_utenti_salva():
    """Crea o modifica un utente con password cifrata.
    La password arriva qui, viene subito trasformata in impronta e NON viene
    mai scritta da nessuna parte."""
    body = request.get_json(force=True) or {}
    nome = str(body.get('nome', '')).strip()
    password = body.get('password')
    ruolo = (str(body.get('ruolo', 'operatore')) or 'operatore').lower()
    zona = (str(body.get('zona', '')) or '').lower().strip()
    if not nome or len(nome) > 40:
        return jsonify({'error': 'Nome utente mancante o troppo lungo.'}), 400
    if any(c in nome for c in ':, '):
        return jsonify({'error': 'Il nome utente non puo contenere due punti, virgole o spazi.'}), 400
    if ruolo not in ('titolare', 'operatore'):
        return jsonify({'error': 'Ruolo non valido.'}), 400
    if zona and zona not in crm_auth.ZONE:
        return jsonify({'error': 'Zona sconosciuta: ' + zona}), 400
    impronta = None
    if password is not None and str(password) != '':
        password = str(password)
        if len(password) < 8:
            return jsonify({'error': 'La password deve essere di almeno 8 caratteri.'}), 400
        impronta = crm_auth.crea_impronta(password)
    try:
        crm_db.utenti_salva(nome, impronta, ruolo, zona)
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    _ricarica_utenti()
    return jsonify({'ok': True, 'utenti': crm_auth.elenco_utenti_visibile()})

@app.route('/api/utenti_elimina', methods=['POST'])
@solo_titolare('gestione_utenti')
def api_utenti_elimina():
    body = request.get_json(force=True) or {}
    nome = str(body.get('nome', '')).strip()
    if not nome:
        return jsonify({'error': 'nome mancante'}), 400
    u = _utente_corrente() or {}
    if nome.lower() == str(u.get('nome', '')).lower():
        return jsonify({'error': 'Non puoi eliminare l utente con cui sei collegato.'}), 400
    # deve restare almeno un titolare gestito dal CRM o dalla variabile
    restanti = [x for x in crm_auth.elenco_utenti_visibile()
                if x['nome'].lower() != nome.lower() and x['ruolo'] == 'titolare']
    if not restanti:
        return jsonify({'error': 'Resteresti senza nessun titolare: eliminazione bloccata.'}), 400
    gestiti = set(x['nome'].lower() for x in crm_db.utenti_lista())
    if nome.lower() not in gestiti:
        return jsonify({'error': 'Questo accesso non e gestito dal CRM: sta nella '
                                 'variabile CRM_UTENTI su Railway e si toglie da li.'}), 400
    crm_db.utenti_elimina(nome)
    _ricarica_utenti()
    return jsonify({'ok': True, 'utenti': crm_auth.elenco_utenti_visibile()})

@app.route('/api/status')
@richiede_login
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
    # Mancavano queste due. 'verifiche' contiene numeri di telefono e note dei
    # casi da controllare di TUTTE le zone; 'accessi' e' il registro di chi si
    # collega, che e' riservato al titolare. Arrivavano entrambi al browser
    # della telefonista dentro /api/load.
    out['verifiche']=[v for v in data.get('verifiche',[])
                      if str(v.get('id') or v.get('ID_contatto') or '') in ids]
    out.pop('accessi', None)
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
# ══════════════════════════════════════════════════════════════════
#  COSA PUO' CAMBIARE UNA TELEFONISTA
# ══════════════════════════════════════════════════════════════════
# Il suo lavoro: consultare, telefonare, fissare richiami e appuntamenti,
# gestire l'agenda per i venditori, scrivere nelle PROPRIE note.
# Tutto il resto della scheda e' in sola lettura: anagrafica, recapiti, note
# storiche, opere, stato del contatto.
#
# E' un elenco CHIUSO, non una lista di divieti: si riparte dal record che sta
# nell'archivio e si applicano SOLO questi campi. Cosi' un campo aggiunto
# domani e' protetto da subito, invece di restare scoperto finche' qualcuno se
# ne accorge. E' l'errore che abbiamo gia' pagato con i nomi delle azioni.
CAMPI_OPERATORE = {
    'Esito_ultima_chiamata',   # com'e' andata la chiamata
    'Prossima_telefonata',     # data del richiamo o dell'appuntamento
    'Ora_appuntamento',        # orario
    'Note_operatore',          # le SUE note: quelle storiche restano intatte
}

def _ruolo_corrente():
    u = _utente_corrente() if crm_auth.USE_AUTH else None
    return (u or {}).get('ruolo', 'titolare')

def _scheda_consentita(nuovo, vecchio, ruolo):
    """Filtra la scheda che arriva dal browser: per una telefonista tiene il
    record a disco e ci applica solo i campi che le competono."""
    if ruolo == 'titolare' or not isinstance(vecchio, dict):
        return nuovo
    fuori = dict(vecchio)
    for k in CAMPI_OPERATORE:
        if k in (nuovo or {}):
            fuori[k] = nuovo[k]
    return fuori

def _regioni_utente():
    """Regioni che l'utente puo' vedere. None = nessun limite (titolare).

    ATTENZIONE: se la zona e' scritta ma sconosciuta (refuso nella variabile
    CRM_UTENTI, zona cancellata) NON si deve tornare None, altrimenti quella
    telefonista vedrebbe TUTTI i contatti d'Italia. Si torna un elenco che non
    corrisponde a nessuna regione: nel dubbio non vede niente, e il problema
    salta all'occhio subito invece di restare nascosto."""
    u=_utente_corrente()
    if u and u.get('zona'):
        return crm_auth.regioni_della_zona(u.get('zona')) or ['(zona sconosciuta)']
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
@solo_titolare('salva_tutto')
def api_save():
    try:
        incoming = request.get_json(force=True)
        # BLOCCO ANTI-AZZERAMENTO (vedi api_save_full)
        if not (incoming.get('contacts') or []):
            return jsonify({'error': 'Salvataggio rifiutato: nessun contatto ricevuto. '
                                     'Ricarica la pagina: i dati sul server sono intatti.'}), 409
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
        save_data(existing, conferma_riduzione=bool(incoming.get('conferma_riduzione')))
        return jsonify({'ok': True, 'contacts': len(existing['contacts'])})
    except crm_db.SalvataggioSospetto as e:
        return jsonify({'error': str(e), 'riduzione': True}), 409
    except Exception as e:
        print(f"Save error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/save_full', methods=['POST'])
@solo_titolare('salva_tutto')
def api_save_full():
    """Called on first load to save everything. Preserves verifiche (and any
    other keys already on disk) so a full re-save never wipes the queue."""
    try:
        incoming = request.get_json(force=True)
        # BLOCCO ANTI-AZZERAMENTO: il client manda sempre le chiavi contacts/
        # telefonate/opere, anche quando in memoria sono vuote perche' il
        # caricamento e' fallito. Accettarle cancellava l'archivio (30/07/2026).
        if not (incoming.get('contacts') or []):
            return jsonify({'error': 'Salvataggio rifiutato: nessun contatto ricevuto. '
                                     'Ricarica la pagina: i dati sul server sono intatti.'}), 409
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
        save_data(existing, conferma_riduzione=bool(incoming.get('conferma_riduzione')))
        return jsonify({'ok': True})
    except crm_db.SalvataggioSospetto as e:
        # 409 e non 500: non e' un guasto, e' un rifiuto motivato. Il client
        # mostra il motivo e offre la conferma, che solo il titolare puo' dare.
        return jsonify({'error': str(e), 'riduzione': True}), 409
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
        # Controllo di zona: mancava, mentre le rotte sorelle lo hanno. Si
        # potevano alterare esito e appuntamenti di qualsiasi contatto.
        _reg = _regioni_utente()
        if _reg:
            _suo = next((x for x in data.get('contacts', [])
                         if str(x.get('ID_contatto')) == cid), None)
            if not _suo or (_suo.get('Regione') or '').strip().lower() \
                    not in set(r.strip().lower() for r in _reg):
                return jsonify({'error': 'contatto fuori dalla tua zona'}), 403
        data.setdefault('telefonate', [])
        data.setdefault('contacts', [])
        # aggiungo la telefonata in testa
        data['telefonate'].insert(0, tel)
        # aggiorno i campi esito del contatto (solo questi, niente altro)
        # Note_telefono = "Stato numero": aggiunto per farlo compilare dalla
        # Registra Telefonata, non solo dalla Modifica (appendice 28).
        campi_ok = {'Esito_ultima_chiamata', 'Prossima_telefonata', 'Ora_appuntamento', 'Note_telefono'}
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


@app.route('/api/salva_contatto', methods=['POST'])
@richiede_login
def api_salva_contatto():
    """Salvataggio INCREMENTALE di un singolo contatto (nuovo o modificato).
    Il client manda SOLO il record interessato invece dell'intero archivio
    (~78 MB di JSON), che su rete normale non arriva mai a destinazione."""
    try:
        body = request.get_json(force=True)
        c = body.get('contatto')
        if not c or not str(c.get('ID_contatto', '')).strip():
            return jsonify({'error': 'contatto mancante o senza ID'}), 400
        cid = str(c['ID_contatto']).strip()
        _reg = _regioni_utente()
        if _reg:
            regset = set(r.strip().lower() for r in _reg)
            if (c.get('Regione') or '').strip().lower() not in regset:
                return jsonify({'error': 'contatto fuori dalla tua zona'}), 403
        data = load_data() or {}
        data.setdefault('contacts', [])
        # La scheda di una telefonista passa dall'elenco chiuso CAMPI_OPERATORE:
        # anagrafica, recapiti, note storiche e opere restano come sono a
        # disco. Controllo sul SERVER: nasconderlo a schermo non basterebbe.
        _ruolo = _ruolo_corrente()
        trovato = False
        for i, x in enumerate(data['contacts']):
            if str(x.get('ID_contatto')) == cid:
                # il controllo di zona va fatto sul record A DISCO, non su
                # quello che arriva: altrimenti basta dichiarare una regione
                # propria per lavorare su un contatto di un'altra zona.
                if _reg and (x.get('Regione') or '').strip().lower() not in regset:
                    return jsonify({'error': 'contatto fuori dalla tua zona'}), 403
                data['contacts'][i] = _scheda_consentita(c, x, _ruolo)
                trovato = True
                break
        if not trovato:
            data['contacts'].insert(0, c)
        save_data(data)
        return jsonify({'ok': True, 'nuovo': not trovato, 'contatti': len(data['contacts'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/salva_scheda', methods=['POST'])
@richiede_login
def api_salva_scheda():
    """Salvataggio INCREMENTALE della scheda di UN contatto: sostituisce le sue
    telefonate e/o le sue opere, lasciando intatto tutto il resto dell'archivio.
    Serve a non spedire i ~67 MB di /api/save_full, che su rete normale
    impiegano oltre 8 minuti e non arrivano mai a destinazione."""
    try:
        body = request.get_json(force=True)
        cid = str(body.get('id_contatto', '')).strip()
        if not cid:
            return jsonify({'error': 'id_contatto mancante'}), 400
        data = load_data() or {}
        # controllo di zona: l'operatore agisce solo sui contatti suoi
        _reg = _regioni_utente()
        if _reg:
            regset = set(r.strip().lower() for r in _reg)
            suo = next((c for c in data.get('contacts', [])
                        if str(c.get('ID_contatto')) == cid), None)
            if not suo or (suo.get('Regione') or '').strip().lower() not in regset:
                return jsonify({'error': 'contatto fuori dalla tua zona'}), 403
        _ruolo = _ruolo_corrente()
        fatti = {}
        for chiave, campo in (('telefonate', 'telefonate'), ('opere', 'opere')):
            if chiave not in body:
                continue                      # assente = non toccare
            if chiave == 'opere' and _ruolo != 'titolare':
                continue   # le opere le registra il titolare, non la telefonista
            nuove = body.get(chiave) or []
            if not isinstance(nuove, list):
                return jsonify({'error': chiave + ' deve essere una lista'}), 400
            altrui = [x for x in (data.get(campo) or [])
                      if str(x.get('ID_contatto')) != cid]
            data[campo] = altrui + nuove
            fatti[chiave] = len(nuove)
        if not fatti:
            return jsonify({'error': 'niente da salvare'}), 400
        # se il client manda anche il contatto aggiornato, lo allineo
        c = body.get('contatto')
        if c and str(c.get('ID_contatto', '')).strip() == cid:
            for i, x in enumerate(data.get('contacts', [])):
                if str(x.get('ID_contatto')) == cid:
                    data['contacts'][i] = _scheda_consentita(c, x, _ruolo)
                    break
        save_data(data)
        return jsonify({'ok': True, 'salvati': fatti})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/bulk_assegna_orari', methods=['POST'])
@richiede_login
def api_bulk_assegna_orari():
    """Assegna Prossima_telefonata/Ora_appuntamento a MOLTI contatti in un
    unico giro (un solo load_data/save_data invece di uno per contatto).
    Nata per il pulsante "Sistema richiami senza ora" dell'agenda: prima
    faceva una chiamata a /api/salva_scheda per contatto (centinaia di
    ricompressioni dell'intero archivio, una dopo l'altra: lento e con
    salvataggi che a volte fallivano a meta'). Qui invece l'archivio viene
    letto e riscritto UNA sola volta per l'intero blocco."""
    try:
        body = request.get_json(force=True)
        aggiornamenti = body.get('aggiornamenti') or []
        if not isinstance(aggiornamenti, list) or not aggiornamenti:
            return jsonify({'error': 'nessun aggiornamento ricevuto'}), 400
        data = load_data() or {}
        by_id = {str(c.get('ID_contatto')): c for c in data.get('contacts', [])}
        # Indice del registro chiamate per contatto: serve per il punto qui sotto.
        tel_by_cid = {}
        for t in data.get('telefonate', []):
            tel_by_cid.setdefault(str(t.get('ID_contatto')), []).append(t)
        _reg = _regioni_utente()
        regset = set(r.strip().lower() for r in _reg) if _reg else None
        aggiornati, non_trovati, fuori_zona = 0, 0, 0
        for u in aggiornamenti:
            cid = str(u.get('id_contatto', '')).strip()
            c = by_id.get(cid)
            if not c:
                non_trovati += 1
                continue
            if regset is not None and (c.get('Regione') or '').strip().lower() not in regset:
                fuori_zona += 1
                continue
            if u.get('data'):
                c['Prossima_telefonata'] = u['data']
            if u.get('ora'):
                c['Ora_appuntamento'] = u['ora']
            # L'agenda (lato client) guarda ANCHE il registro chiamate: se per
            # questo contatto esiste gia' una voce con la stessa data e senza
            # ora, quella voce "vince" su quanto appena assegnato al contatto
            # e la riga resta comunque in "Tutto il giorno". Allineamo quindi
            # anche le voci del registro sulla stessa data.
            if u.get('data') and u.get('ora'):
                for t in tel_by_cid.get(cid, []):
                    if t.get('Data_appuntamento') != u['data']:
                        continue
                    if t.get('Ora_appuntamento'):
                        continue
                    es = (t.get('Esito') or '').strip().lower()
                    if es == 'appuntamento' or 'richiamo' in es or es == 'richiamare':
                        t['Ora_appuntamento'] = u['ora']
            aggiornati += 1
        if aggiornati:
            save_data(data)
        return jsonify({'ok': True, 'aggiornati': aggiornati,
                        'non_trovati': non_trovati, 'fuori_zona': fuori_zona})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/elimina_contatti', methods=['POST'])
@solo_titolare('elimina')
def api_elimina_contatti():
    """Elimina PIU' contatti in un colpo solo (24/09/2026, pulizia librerie).
    Una sola lettura e una sola scrittura dell'archivio, invece di 200 giri
    da 7 MB l'uno. Prima di scrivere viene messa da parte una copia da evento
    ("prima della riduzione — ..."): si ritrova in Copie di sicurezza e con
    Ripristina si torna indietro. Si rifiuta di toccare un contatto che ha
    opere comprate: quelle schede non sono mai "da buttare" in blocco."""
    try:
        body = request.get_json(force=True) or {}
        ids = [str(x).strip() for x in (body.get('ids') or []) if str(x).strip()]
        motivo = str(body.get('motivo', '') or '').strip()[:60]
        if not ids:
            return jsonify({'error': 'nessun contatto indicato'}), 400
        if len(ids) > 500:
            return jsonify({'error': 'troppi contatti in una volta (massimo 500)'}), 400
        data = load_data() or {}
        chiesti = set(ids)
        con_opere = set(str(o.get('ID_contatto')) for o in (data.get('opere') or [])
                        if str(o.get('ID_contatto')) in chiesti)
        da_togliere = chiesti - con_opere
        presenti = set(str(c.get('ID_contatto')) for c in data.get('contacts', [])
                       if str(c.get('ID_contatto')) in da_togliere)
        if not presenti:
            return jsonify({'error': 'nessuno dei contatti indicati e\' eliminabile',
                            'con_opere': sorted(con_opere)}), 400
        tolti = {'contatti': len(presenti)}
        data['contacts'] = [c for c in data.get('contacts', [])
                            if str(c.get('ID_contatto')) not in presenti]
        for campo in ('telefonate', 'verifiche'):
            prima = data.get(campo) or []
            dopo = [x for x in prima if str(x.get('ID_contatto')) not in presenti]
            if len(dopo) != len(prima):
                data[campo] = dopo
                tolti[campo] = len(prima) - len(dopo)
        save_data(data, copia_prima=('eliminati %d contatti %s' % (len(presenti), motivo)).strip())
        return jsonify({'ok': True, 'eliminati': tolti,
                        'non_trovati': sorted(da_togliere - presenti),
                        'saltati_con_opere': sorted(con_opere),
                        'contatti_rimasti': len(data['contacts'])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/elimina_contatto', methods=['POST'])
@solo_titolare('elimina')
def api_elimina_contatto():
    # 11/09/2026: prima qui c'era solo @richiede_login. Il pulsante Elimina
    # era nascosto alle telefoniste dal CSS e bloccato dal JS, ma la rotta
    # accettava comunque la richiesta: bastava la console del browser per
    # cancellare un contatto della propria zona. Ora il controllo e' sul
    # server, dove nessuno puo' aggirarlo.
    """Elimina UN contatto e tutto cio' che gli e' collegato, lato server.
    Evita di rispedire l'intero archivio con /api/save_full."""
    try:
        cid = str((request.get_json(force=True) or {}).get('id_contatto', '')).strip()
        if not cid:
            return jsonify({'error': 'id_contatto mancante'}), 400
        data = load_data() or {}
        c = next((x for x in data.get('contacts', [])
                  if str(x.get('ID_contatto')) == cid), None)
        if not c:
            return jsonify({'error': 'contatto non trovato'}), 404
        _reg = _regioni_utente()
        if _reg:
            regset = set(r.strip().lower() for r in _reg)
            if (c.get('Regione') or '').strip().lower() not in regset:
                return jsonify({'error': 'contatto fuori dalla tua zona'}), 403
        tolti = {}
        data['contacts'] = [x for x in data.get('contacts', [])
                            if str(x.get('ID_contatto')) != cid]
        tolti['contatto'] = 1
        for campo in ('telefonate', 'opere', 'verifiche'):
            prima = data.get(campo) or []
            dopo = [x for x in prima if str(x.get('ID_contatto')) != cid]
            if len(dopo) != len(prima):
                data[campo] = dopo
                tolti[campo] = len(prima) - len(dopo)
        save_data(data)
        return jsonify({'ok': True, 'eliminati': tolti,
                        'contatti_rimasti': len(data['contacts'])})
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
    # forza=True: e' l'unico azzeramento voluto, deciso dal titolare.
    # NOTA: sul database online resta comunque attivo il trigger
    # trg_guard_crm_blob, che rifiuta le scritture che dimezzano i dati.
    save_data({}, forza=True)
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
@richiede_login
def api_pdf():
    # 11/09/2026: era senza login. Accetta una lista di ID e restituisce le
    # schede complete (nome, indirizzo, codice fiscale, telefoni): chiunque
    # poteva svuotare l'archivio un blocco alla volta.
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
        # Niente stampe di massa per le telefoniste: la soglia era dichiarata
        # in crm_auth (LIMITE_STAMPA_MASSA) e non veniva applicata da nessuno,
        # cosi' si potevano estrarre 3.000 schede complete con indirizzo, data
        # di nascita e codice fiscale.
        if _ruolo_corrente() != 'titolare' and len(ids) > crm_auth.LIMITE_STAMPA_MASSA:
            return jsonify({'error': 'Puoi stampare al massimo %d schede alla volta. '
                                     'Le stampe di massa sono riservate al titolare.'
                                     % crm_auth.LIMITE_STAMPA_MASSA}), 403
        data = load_data()
        _reg = _regioni_utente()
        if _reg:
            data = _filtra_per_zona(data, _reg)   # l'operatore stampa solo la sua zona
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
@richiede_login
def api_zona():
    """Restituisce i contatti vicini all'appuntamento entro un raggio (km),
    geocodificando i comuni della provincia solo se non già in cache."""
    try:
        body = request.get_json(force=True)
        center_id = str(body.get('id', ''))
        radius = float(body.get('radius', 10))
        data = load_data()
        _reg = _regioni_utente()
        if _reg:
            data = _filtra_per_zona(data, _reg)
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
@richiede_login
def api_zona_libera():
    """Contatti vicini a un LUOGO digitato (zona libera), entro un raggio (km).
    Geocodifica i comuni man mano (cache condivisa con /api/zona)."""
    try:
        body = request.get_json(force=True)
        place = str(body.get('place', '')).strip()
        radius = float(body.get('radius', 15))
        radius = min(max(radius, 1.0), 200.0)   # tetto: niente "raggio 9999" per prendere tutto
        prov_hint = str(body.get('prov', '')).strip().upper()  # opzionale: limita la provincia
        if not place:
            return jsonify({'error': 'scrivi un luogo'}), 400

        gp = geocode_place(place)
        time.sleep(1.1)
        if not gp:
            return jsonify({'error': f'non trovo il luogo "{place}"'}), 200
        cpt = (gp[0], gp[1])

        data = load_data()
        # FILTRO DI ZONA: c'era in /api/zona ma NON qui (trovato il 12/09/2026).
        # Senza, una telefonista poteva scrivere una citta' di un'altra zona e
        # ricevere nome, indirizzo e telefoni di quei contatti: bastava ripetere
        # provincia per provincia per scaricarsi l'intera rubrica.
        _reg = _regioni_utente()
        if _reg:
            data = _filtra_per_zona(data, _reg)
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
@richiede_login
def api_verifica():
    # 11/09/2026: mancava @richiede_login (rotta aperta a chiunque).
    # Le azioni 'elimina' e 'unisci' cancellano schede: riservate al titolare.
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
        if azione in ('elimina', 'unisci') and not _solo_titolare_api():
            return jsonify({'error': 'Unire o eliminare schede e riservato al titolare.'}), 403
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
@richiede_login
def api_contatto_stato():
    """Conferma (stato=attivo) o segnala (stato=da_verificare) un contatto."""
    try:
        body = request.get_json(force=True)
        cid = str(body.get('id', '')); stato = str(body.get('stato', 'attivo')).strip()
        # Elenco CHIUSO: 'Stato' e' il campo che toglie un contatto dalla
        # lavorazione. Prima accettava qualunque valore, anche un oggetto.
        if stato not in STATI_VALIDI:
            return jsonify({'error': 'stato non valido'}), 400
        data = load_data()
        c = next((x for x in data.get('contacts', []) if str(x.get('ID_contatto')) == cid), None)
        if not c:
            return jsonify({'error': 'contatto non trovato'}), 404
        # Controllo di zona: mancava del tutto, si poteva "spegnere" qualunque
        # contatto d'Italia.
        _reg = _regioni_utente()
        if _reg and (c.get('Regione') or '').strip().lower() not in set(r.strip().lower() for r in _reg):
            return jsonify({'error': 'contatto fuori dalla tua zona'}), 403
        c['Stato'] = stato
        save_data(data)
        return jsonify({'ok': True, 'id': cid, 'stato': stato})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/segnala', methods=['POST'])
@richiede_login
def api_segnala():
    """Aggiunge un contatto alla coda 'Da controllare' e lo mette in stato da_verificare."""
    try:
        body = request.get_json(force=True)
        cid = str(body.get('id', ''))
        # testo libero, ma non illimitato: prima si potevano accodare voci
        # enormi dentro l'archivio
        tipo = str(body.get('tipo', 'segnalato')).strip()[:60]
        nota = str(body.get('nota', ''))[:500]
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
