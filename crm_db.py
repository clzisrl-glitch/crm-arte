#!/usr/bin/env python3
"""
crm_db.py — Strato dati del CRM.

Funziona in DUE modi, scelti AUTOMATICAMENTE:
  • ONLINE  (Railway): se esiste la variabile d'ambiente DATABASE_URL,
    usa un database PostgreSQL vero (i dati non si perdono ai riavvii).
  • LOCALE  (il tuo Mac): se NON c'è DATABASE_URL, usa il file crm_data.json
    come ha sempre fatto (nessun cambiamento per te in locale).

Il resto del programma chiama solo load_data() e save_data(): non sa
e non gli importa quale dei due modi sia attivo.
"""
import os, json, datetime, hashlib
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / 'crm_data.json'
BACKUP_DIR = BASE_DIR / 'backup'
MAX_BACKUPS = 30

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_DB = bool(DATABASE_URL)

# ─────────────────────────────────────────────────────────────
#  MODO ONLINE — PostgreSQL (Railway)
# ─────────────────────────────────────────────────────────────
_pg = None
def _get_pg():
    """Connessione PostgreSQL (psycopg 3). I dati del CRM stanno COMPRESSI
    (gzip) in un'unica riga della tabella crm_blob: cosi invece di ~70MB
    se ne trasmettono ~7MB, evitando le interruzioni SSL su payload grandi."""
    global _pg
    import psycopg
    url = DATABASE_URL
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    if _pg is None or _pg.closed:
        # keepalives: tiene viva la connessione durante scritture grandi
        _pg = psycopg.connect(url, autocommit=True,
                              keepalives=1, keepalives_idle=30,
                              keepalives_interval=10, keepalives_count=5)
    return _pg

def _db_init():
    conn = _get_pg()
    with conn.cursor() as cur:
        # BYTEA = dati binari (qui ci mettiamo il JSON compresso gzip)
        cur.execute("CREATE TABLE IF NOT EXISTS crm_blob (id INT PRIMARY KEY, data BYTEA)")
        cur.execute("CREATE TABLE IF NOT EXISTS crm_backup (giorno TEXT PRIMARY KEY, data BYTEA, creato TIMESTAMP DEFAULT now())")

def _comprimi(data):
    import gzip as _g
    # Livello 6, non 9 (default). Misurato sull'archivio reale (74 MB):
    #   livello 9 -> 4,99 s per 6,77 MB
    #   livello 6 -> 0,88 s per 7,14 MB
    # Quattro secondi di CPU a ogni salvataggio per risparmiare 370 KB non
    # conviene: lo spazio non e' un problema, il tempo di scrittura si'.
    return _g.compress(json.dumps(data, ensure_ascii=False, separators=(',',':')).encode('utf-8'), 6)

class DatiIllegibili(Exception):
    """I dati ci sono ma non si riescono a leggere.
    NON significa 'database vuoto': chi la riceve NON deve mai concludere
    che l'archivio sia da ricreare."""
    pass

def _decomprimi(blob):
    import gzip as _g
    if blob is None:
        return {}
    b = bytes(blob)
    # se per qualche motivo non e' compresso (vecchio formato), provo a leggerlo come testo
    try:
        return json.loads(_g.decompress(b).decode('utf-8'))
    except Exception as _e1:
        try:
            return json.loads(b.decode('utf-8'))
        except Exception as _e2:
            # PRIMA qui c'era 'return {}': un errore di lettura diventava
            # silenziosamente "database vuoto", e il primo salvataggio
            # successivo rendeva quel vuoto definitivo (incidente 30/07/2026).
            raise DatiIllegibili(
                f"blob di {len(b)} byte non decodificabile ({_e1} / {_e2})")

def _db_load():
    _db_init()
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM crm_blob WHERE id=1")
        row = cur.fetchone()
        if row and row[0] is not None:
            return _decomprimi(row[0])
    return {}

class SalvataggioSospetto(Exception):
    """Salvataggio rifiutato perche' cancellerebbe l'archivio."""
    pass

def _controlla_payload(data, forza):
    """Un salvataggio senza contatti non e' mai legittimo, tranne il reset
    esplicito del titolare (forza=True). Senza questo controllo, un
    caricamento fallito si trasformava in un archivio azzerato."""
    if forza:
        return
    if not isinstance(data, dict) or not (data.get('contacts') or []):
        raise SalvataggioSospetto(
            "salvataggio rifiutato: zero contatti. "
            "Quasi sempre significa che il caricamento iniziale non e' riuscito. "
            "I dati sul database NON sono stati toccati.")

import contextlib as _ctx

@_ctx.contextmanager
def blocco_scrittura():
    """Serializza il ciclo leggi-modifica-scrivi fra TUTTI i worker.
    Senza questo, due operatori che salvano nello stesso momento leggono
    entrambi la stessa versione dell'archivio e il secondo che scrive
    CANCELLA le modifiche del primo, senza nessun errore visibile.
    Usa un advisory lock di PostgreSQL: vale fra processi diversi.
    In modalita' locale (file) non serve: un solo processo."""
    if not USE_DB:
        yield
        return
    conn = _get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(738104)")
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(738104)")
        except Exception:
            pass

_last_db_backup_day = None
def _db_save(data, forza=False):
    global _last_db_backup_day
    _controlla_payload(data, forza)
    _db_init()
    conn = _get_pg()
    payload = _comprimi(data)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO crm_blob (id, data) VALUES (1, %s) "
                    "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data", (payload,))
        # backup: una copia al giorno
        giorno = datetime.datetime.now().strftime('%Y-%m-%d')
        if giorno != _last_db_backup_day:
            cur.execute("INSERT INTO crm_backup (giorno, data) VALUES (%s, %s) "
                        "ON CONFLICT (giorno) DO UPDATE SET data = EXCLUDED.data, creato = now()", (giorno, payload))
            # conservo solo gli ultimi MAX_BACKUPS giorni
            cur.execute("DELETE FROM crm_backup WHERE giorno NOT IN "
                        "(SELECT giorno FROM crm_backup ORDER BY giorno DESC LIMIT %s)", (MAX_BACKUPS,))
            _last_db_backup_day = giorno

def _db_has_data():
    """Attenzione: se la lettura FALLISCE l'eccezione esce di proposito.
    Prima veniva catturata e si rispondeva False ('vuoto'), il che faceva
    scattare la reimportazione del file di seed sopra ai dati veri."""
    d = _db_load()
    return bool(d.get('contacts'))

# ─────────────────────────────────────────────────────────────
#  MODO LOCALE — file crm_data.json (come sempre)
# ─────────────────────────────────────────────────────────────
_last_backup_hash = None
def _file_auto_backup(text):
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

def _file_load():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore lettura dati: {e}")
            try:
                files = sorted((BACKUP_DIR).glob('crm_data_*.json'))
                if files:
                    print(f"  RECUPERO dall'ultimo backup: {files[-1].name}")
                    with open(files[-1], 'r', encoding='utf-8') as f:
                        return json.load(f)
            except Exception as e2:
                print(f"  backup non recuperabile: {e2}")
    return {}

def _file_save(data):
    text = json.dumps(data, ensure_ascii=False, separators=(',',':'))
    tmp = DATA_FILE.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)
    _file_auto_backup(text)

# ─────────────────────────────────────────────────────────────
#  INTERFACCIA UNICA (quello che usa il server)
# ─────────────────────────────────────────────────────────────
def load_data():
    return _db_load() if USE_DB else _file_load()

def save_data(data, forza=False):
    """forza=True SOLO per il reset esplicito del titolare."""
    if USE_DB:
        return _db_save(data, forza=forza)
    _controlla_payload(data, forza)   # stessa protezione anche in locale
    return _file_save(data)

def has_data():
    return _db_has_data() if USE_DB else DATA_FILE.exists()

def modo():
    return 'DATABASE PostgreSQL (online)' if USE_DB else 'file locale crm_data.json'

# Caricamento iniziale dei dati nel database, da file, una sola volta.
# Si usa al primo avvio online: se il DB è vuoto e c'è il file, lo importa.
def _forza_reset_tabelle():
    """Cancella le tabelle se sono di vecchio formato (jsonb invece di bytea).
    Usa una connessione fresca e controlla il tipo della colonna 'data'."""
    import psycopg
    url = DATABASE_URL
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    conn = psycopg.connect(url, autocommit=True,
                           keepalives=1, keepalives_idle=30,
                           keepalives_interval=10, keepalives_count=5)
    try:
        with conn.cursor() as cur:
            # controllo il tipo della colonna data in crm_blob
            cur.execute("""SELECT data_type FROM information_schema.columns
                           WHERE table_name='crm_blob' AND column_name='data'""")
            row = cur.fetchone()
            tipo = (row[0] if row else '').lower()
            # Se non e' bytea (es. jsonb/json vecchio) la tabella va sostituita.
            # PRIMA qui c'era: DROP TABLE crm_blob + DROP TABLE crm_backup.
            # Due difetti gravi: (1) distruggeva i dati senza possibilita' di
            # recupero; (2) cancellava ANCHE crm_backup, cioe' tutti i backup,
            # in base al tipo di una colonna di un'ALTRA tabella.
            # Ora si RINOMINA: niente viene perso e si puo' recuperare a mano.
            if tipo and tipo != 'bytea':
                import time as _t
                suff = _t.strftime('%Y%m%d_%H%M%S')
                cur.execute(f'ALTER TABLE crm_blob RENAME TO crm_blob_vecchia_{suff}')
                print(f"  ATTENZIONE: crm_blob era di tipo '{tipo}', non bytea.")
                print(f"  NON e' stata cancellata: rinominata in crm_blob_vecchia_{suff}.")
                print(f"  I backup (crm_backup) NON sono stati toccati.")
            # ricreo se mancano (formato corretto bytea)
            cur.execute("CREATE TABLE IF NOT EXISTS crm_blob (id INT PRIMARY KEY, data BYTEA)")
            cur.execute("CREATE TABLE IF NOT EXISTS crm_backup (giorno TEXT PRIMARY KEY, data BYTEA, creato TIMESTAMP DEFAULT now())")
    finally:
        conn.close()

def seed_from_file_if_empty():
    if not USE_DB:
        return False
    try:
        # assicuro che le tabelle siano del formato giusto (bytea); ricreo se vecchie
        try:
            _forza_reset_tabelle()
        except Exception as _e0:
            print(f"  (controllo tabelle: {_e0})")
        # ora controllo se ci sono gia dati validi
        try:
            if _db_has_data():
                return False
        except Exception as _ec:
            # PRIMA qui c'era 'pass  # proseguo a importare': se il controllo
            # falliva si reimportava il file di seed SOPRA i dati veri.
            # Se non riusciamo a sapere cosa c'e' nel database, non si tocca.
            print(f"  ATTENZIONE: controllo dati non riuscito ({_ec}).")
            print("  Primo caricamento ANNULLATO per non sovrascrivere dati esistenti.")
            return False
        d = None
        # 1) provo dal file compresso crm_data.json.gz (per l'online, piccolo abbastanza per GitHub)
        gz = BASE_DIR / 'crm_data.json.gz'
        if gz.exists():
            import gzip
            with gzip.open(gz, 'rt', encoding='utf-8') as f:
                d = json.load(f)
        # 2) altrimenti dal file normale
        elif DATA_FILE.exists():
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
        if d and d.get('contacts'):
            _db_save(d)
            print(f"  PRIMO CARICAMENTO: {len(d['contacts'])} contatti importati nel database.")
            return True
        else:
            print("  (primo caricamento: file dati non trovato o vuoto)")
    except Exception as e:
        print(f"  (primo caricamento non riuscito: {e})")
    return False


# ─────────────────────────────────────────────────────────────
#  BACKUP: lista e lettura delle copie giornaliere (per la pagina Backup admin)
# ─────────────────────────────────────────────────────────────
def lista_backup():
    """Ritorna la lista dei backup giornalieri disponibili: [{giorno, creato}], più recenti prima."""
    if not USE_DB:
        # in locale: elenco i file nella cartella backup
        try:
            files = sorted(BACKUP_DIR.glob('crm_data_*.json'), reverse=True)
            return [{'giorno': f.stem.replace('crm_data_', ''), 'creato': ''} for f in files]
        except Exception:
            return []
    try:
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT giorno, creato FROM crm_backup ORDER BY giorno DESC")
            righe = cur.fetchall()
        return [{'giorno': r[0], 'creato': str(r[1]) if r[1] else ''} for r in righe]
    except Exception as e:
        print(f"  (lista_backup: {e})")
        return []

def carica_backup(giorno):
    """Ritorna i dati di un backup giornaliero specifico (dict), o None."""
    if not USE_DB:
        try:
            f = BACKUP_DIR / f'crm_data_{giorno}.json'
            if f.exists():
                with open(f, 'r', encoding='utf-8') as fh:
                    return json.load(fh)
        except Exception:
            return None
        return None
    try:
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM crm_backup WHERE giorno = %s", (giorno,))
            row = cur.fetchone()
        if row and row[0]:
            return _decomprimi(bytes(row[0]))
    except Exception as e:
        print(f"  (carica_backup: {e})")
    return None


# ─────────────────────────────────────────────────────────────
#  STATO INTERFACCIA PER UTENTE  (ultima scheda aperta)
# ─────────────────────────────────────────────────────────────
# Perche' NON sta nel blob dei dati:
#   • il blob (crm_blob) pesa ~7 MB: riscriverlo a ogni cambio di scheda
#     sarebbe lentissimo e metterebbe a rischio l'archivio per una
#     sciocchezza (una preferenza di interfaccia).
#   • localStorage e' vietato in questo progetto: la preferenza deve
#     seguire l'utente su tutti i dispositivi, quindi sta sul server.
# Soluzione: una tabella piccola e separata (crm_ui), una riga per utente.
# In locale (Mac, senza DATABASE_URL) si usa un file JSON minuscolo.
UI_FILE = BASE_DIR / 'ui_stato.json'
UI_MAX_BYTE = 4000          # una preferenza non puo' diventare un archivio

def _ui_db_init():
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS crm_ui ("
                    "utente TEXT PRIMARY KEY, dati TEXT, "
                    "aggiornato TIMESTAMP DEFAULT now())")

def _ui_chiave(utente):
    return ((utente or 'locale').strip().lower() or 'locale')[:80]

def ui_get(utente):
    """Stato interfaccia salvato per questo utente (dict, mai None)."""
    u = _ui_chiave(utente)
    try:
        if USE_DB:
            _ui_db_init()
            conn = _get_pg()
            with conn.cursor() as cur:
                cur.execute("SELECT dati FROM crm_ui WHERE utente = %s", (u,))
                row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0]) or {}
            return {}
        if UI_FILE.exists():
            tutto = json.loads(UI_FILE.read_text(encoding='utf-8')) or {}
            return tutto.get(u) or {}
    except Exception as e:
        print(f"  (ui_get: {e})")
    return {}

def ui_set(utente, dati):
    """Salva lo stato interfaccia di questo utente. Non tocca mai i dati CRM."""
    u = _ui_chiave(utente)
    testo = json.dumps(dati or {}, ensure_ascii=False)
    if len(testo.encode('utf-8')) > UI_MAX_BYTE:
        raise ValueError('stato interfaccia troppo grande')
    if USE_DB:
        _ui_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_ui (utente, dati, aggiornato) VALUES (%s, %s, now()) "
                "ON CONFLICT (utente) DO UPDATE SET dati = EXCLUDED.dati, aggiornato = now()",
                (u, testo))
        return True
    tutto = {}
    try:
        if UI_FILE.exists():
            tutto = json.loads(UI_FILE.read_text(encoding='utf-8')) or {}
    except Exception:
        tutto = {}
    tutto[u] = dati or {}
    UI_FILE.write_text(json.dumps(tutto, ensure_ascii=False), encoding='utf-8')
    return True


# ─────────────────────────────────────────────────────────────
#  UTENTI DEL CRM (password cifrate)
# ─────────────────────────────────────────────────────────────
# Tabella piccola e separata dall'archivio, come crm_ui: gli utenti non
# stanno nel blob dei dati, cosi' crearne uno non riscrive i ~7MB e un
# guasto qui non tocca i contatti.
# Nella colonna "impronta" NON c'e' la password: c'e' il risultato di un
# calcolo a senso unico (PBKDF2, vedi crm_auth.py). Dall'impronta non si
# risale alla password.
UTENTI_FILE = BASE_DIR / 'utenti_crm.json'

def _utenti_db_init():
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS crm_utenti ("
                    "nome TEXT PRIMARY KEY, impronta TEXT NOT NULL, "
                    "ruolo TEXT NOT NULL, zona TEXT DEFAULT '', "
                    "aggiornato TIMESTAMP DEFAULT now())")

def utenti_lista():
    """Tutti gli utenti gestiti dal CRM. Ritorna [] se non ce n'e' nessuno."""
    try:
        if USE_DB:
            _utenti_db_init()
            conn = _get_pg()
            with conn.cursor() as cur:
                cur.execute("SELECT nome, impronta, ruolo, zona FROM crm_utenti ORDER BY nome")
                righe = cur.fetchall()
            return [{'nome': r[0], 'impronta': r[1], 'ruolo': r[2], 'zona': r[3] or ''}
                    for r in righe]
        if UTENTI_FILE.exists():
            return json.loads(UTENTI_FILE.read_text(encoding='utf-8')) or []
    except Exception as e:
        print(f"  (utenti_lista: {e})")
    return []

def utenti_salva(nome, impronta, ruolo, zona=''):
    """Crea o aggiorna un utente. Se impronta e' None tiene quella esistente
    (serve per cambiare solo ruolo o zona senza toccare la password)."""
    nome = (nome or '').strip()
    if not nome:
        raise ValueError('nome utente mancante')
    if impronta is None:
        esistente = next((u for u in utenti_lista() if u['nome'].lower() == nome.lower()), None)
        if not esistente:
            raise ValueError('utente non trovato: serve una password')
        impronta = esistente['impronta']
    ruolo = (ruolo or 'operatore').lower()
    zona = (zona or '').lower()
    if USE_DB:
        _utenti_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_utenti (nome, impronta, ruolo, zona, aggiornato) "
                "VALUES (%s, %s, %s, %s, now()) "
                "ON CONFLICT (nome) DO UPDATE SET impronta = EXCLUDED.impronta, "
                "ruolo = EXCLUDED.ruolo, zona = EXCLUDED.zona, aggiornato = now()",
                (nome, impronta, ruolo, zona))
        return True
    elenco = [u for u in utenti_lista() if u['nome'].lower() != nome.lower()]
    elenco.append({'nome': nome, 'impronta': impronta, 'ruolo': ruolo, 'zona': zona})
    UTENTI_FILE.write_text(json.dumps(elenco, ensure_ascii=False), encoding='utf-8')
    return True

def utenti_elimina(nome):
    nome = (nome or '').strip()
    if not nome:
        return False
    if USE_DB:
        _utenti_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_utenti WHERE lower(nome) = lower(%s)", (nome,))
        return True
    elenco = [u for u in utenti_lista() if u['nome'].lower() != nome.lower()]
    UTENTI_FILE.write_text(json.dumps(elenco, ensure_ascii=False), encoding='utf-8')
    return True


# ═══════════════════════════════════════════════════════════════════
#  REGISTRO ATTIVITA' E TEMPO DI CONNESSIONE
# ═══════════════════════════════════════════════════════════════════
# Due tabelle PICCOLE e separate, come crm_ui e crm_utenti. NON dentro il
# blob dei dati: registrare un'operazione non deve riscrivere i ~7 MB
# dell'archivio (e' l'errore che avrebbe reso il CRM inutilizzabile).
#   crm_attivita  = cosa e' stato fatto, da chi, quando, su quale scheda
#   crm_sessioni  = da quando a quando ciascuno e' stato collegato
ATTIVITA_MAX = 30000        # righe conservate nel registro
SESSIONE_PAUSA_MIN = 15     # dopo tanti minuti di fermo la sessione e' chiusa

def _att_db_init():
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS crm_attivita ("
                    "id BIGSERIAL PRIMARY KEY, nome TEXT, ruolo TEXT, zona TEXT, "
                    "azione TEXT, id_contatto TEXT, dettaglio TEXT, "
                    "quando TIMESTAMPTZ DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS crm_attivita_quando ON crm_attivita (quando DESC)")
        cur.execute("CREATE TABLE IF NOT EXISTS crm_sessioni ("
                    "id BIGSERIAL PRIMARY KEY, nome TEXT, ruolo TEXT, zona TEXT, "
                    "inizio TIMESTAMPTZ DEFAULT now(), ultimo TIMESTAMPTZ DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS crm_sessioni_nome ON crm_sessioni (nome, ultimo DESC)")

def attivita_registra(nome, ruolo, zona, azione, id_contatto='', dettaglio=''):
    """Scrive una riga nel registro. Non deve MAI far fallire l'operazione
    dell'utente: se il registro non si scrive, pazienza."""
    if not USE_DB:
        return False
    try:
        _att_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO crm_attivita (nome, ruolo, zona, azione, id_contatto, dettaglio) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (str(nome or '')[:80], str(ruolo or '')[:20], str(zona or '')[:40],
                         str(azione or '')[:60], str(id_contatto or '')[:40],
                         str(dettaglio or '')[:200]))
            # potatura: tengo le ultime ATTIVITA_MAX righe
            cur.execute("DELETE FROM crm_attivita WHERE id < "
                        "(SELECT COALESCE(MIN(id),0) FROM (SELECT id FROM crm_attivita "
                        " ORDER BY id DESC LIMIT %s) t)", (ATTIVITA_MAX,))
        return True
    except Exception as e:
        print(f"  (attivita_registra: {e})")
        return False

def presenza_tocca(nome, ruolo, zona):
    """Segna che l'utente e' vivo adesso. Se la sua ultima attivita' e' di
    piu' di SESSIONE_PAUSA_MIN minuti, apre una sessione NUOVA: cosi' il
    tempo di connessione non conta le ore in cui il CRM era solo aperto."""
    if not USE_DB:
        return False
    try:
        _att_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM crm_sessioni WHERE nome=%s "
                        "AND ultimo > now() - (%s || ' minutes')::interval "
                        "ORDER BY ultimo DESC LIMIT 1",
                        (str(nome or '')[:80], str(SESSIONE_PAUSA_MIN)))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE crm_sessioni SET ultimo=now() WHERE id=%s", (r[0],))
            else:
                cur.execute("INSERT INTO crm_sessioni (nome, ruolo, zona) VALUES (%s,%s,%s)",
                            (str(nome or '')[:80], str(ruolo or '')[:20], str(zona or '')[:40]))
        return True
    except Exception as e:
        print(f"  (presenza_tocca: {e})")
        return False

def attivita_elenco(giorni=7, limite=400):
    """Ultime operazioni registrate, dalla piu' recente."""
    if not USE_DB:
        return []
    try:
        _att_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT nome, ruolo, zona, azione, id_contatto, dettaglio, quando "
                        "FROM crm_attivita WHERE quando > now() - (%s || ' days')::interval "
                        "ORDER BY id DESC LIMIT %s", (str(int(giorni)), int(limite)))
            return [{'nome': a, 'ruolo': b, 'zona': c, 'azione': d, 'id_contatto': e,
                     'dettaglio': f, 'quando': g.isoformat(timespec='seconds') if g else ''}
                    for a, b, c, d, e, f, g in cur.fetchall()]
    except Exception as e:
        print(f"  (attivita_elenco: {e})")
        return []

def sessioni_elenco(giorni=7):
    """Sessioni di collegamento, dalla piu' recente, con la durata in minuti."""
    if not USE_DB:
        return []
    try:
        _att_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT nome, ruolo, zona, inizio, ultimo, "
                        "  GREATEST(1, ROUND(EXTRACT(EPOCH FROM (ultimo-inizio))/60)::int), "
                        "  (ultimo > now() - interval '5 minutes') "
                        "FROM crm_sessioni WHERE inizio > now() - (%s || ' days')::interval "
                        "ORDER BY inizio DESC LIMIT 500", (str(int(giorni)),))
            return [{'nome': a, 'ruolo': b, 'zona': c,
                     'inizio': d.isoformat(timespec='seconds') if d else '',
                     'ultimo': e.isoformat(timespec='seconds') if e else '',
                     'minuti': int(f or 0), 'collegato': bool(g)}
                    for a, b, c, d, e, f, g in cur.fetchall()]
    except Exception as e:
        print(f"  (sessioni_elenco: {e})")
        return []


# ═══════════════════════════════════════════════════════════════════
#  MESSAGGI FRA TELEFONISTA E TITOLARE
# ═══════════════════════════════════════════════════════════════════
# Una conversazione per ogni telefonista, con il titolare. Serve a chiedere le
# modifiche che lei non puo' fare (un indirizzo sbagliato, una nota da
# correggere) senza telefonate o messaggi che si perdono.
# Tabella piccola e separata, come le altre: NON dentro il blob dei dati.
#   utente     = di chi e' la conversazione (sempre il nome della telefonista)
#   da_titolare= chi ha scritto: vero = titolare, falso = la telefonista
#   id_contatto= la scheda di cui si parla (facoltativo, cliccabile)
MESSAGGI_MAX = 5000

def _msg_db_init():
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS crm_messaggi ("
                    "id BIGSERIAL PRIMARY KEY, utente TEXT NOT NULL, "
                    "da_titolare BOOLEAN DEFAULT false, autore TEXT, "
                    "testo TEXT NOT NULL, id_contatto TEXT DEFAULT '', "
                    "letto BOOLEAN DEFAULT false, quando TIMESTAMPTZ DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS crm_messaggi_utente "
                    "ON crm_messaggi (utente, id DESC)")

def messaggio_scrivi(utente, da_titolare, autore, testo, id_contatto=''):
    if not USE_DB:
        return 0
    try:
        _msg_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO crm_messaggi (utente, da_titolare, autore, testo, id_contatto) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (str(utente or '')[:80], bool(da_titolare), str(autore or '')[:80],
                         str(testo or '')[:2000], str(id_contatto or '')[:40]))
            nuovo = cur.fetchone()[0]
            cur.execute("DELETE FROM crm_messaggi WHERE id < "
                        "(SELECT COALESCE(MIN(id),0) FROM (SELECT id FROM crm_messaggi "
                        " ORDER BY id DESC LIMIT %s) t)", (MESSAGGI_MAX,))
            return int(nuovo)
    except Exception as e:
        print(f"  (messaggio_scrivi: {e})")
        return 0

def messaggi_elenco(utente=None, limite=200):
    """Se utente e' indicato, la sua conversazione. Altrimenti tutte."""
    if not USE_DB:
        return []
    try:
        _msg_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            if utente:
                cur.execute("SELECT id, utente, da_titolare, autore, testo, id_contatto, letto, quando "
                            "FROM crm_messaggi WHERE utente=%s ORDER BY id DESC LIMIT %s",
                            (str(utente)[:80], int(limite)))
            else:
                cur.execute("SELECT id, utente, da_titolare, autore, testo, id_contatto, letto, quando "
                            "FROM crm_messaggi ORDER BY id DESC LIMIT %s", (int(limite),))
            righe = cur.fetchall()
        return [{'id': int(a), 'utente': b, 'da_titolare': bool(c), 'autore': d, 'testo': e,
                 'id_contatto': f, 'letto': bool(g),
                 'quando': h.isoformat(timespec='seconds') if h else ''}
                for a, b, c, d, e, f, g, h in reversed(righe)]
    except Exception as e:
        print(f"  (messaggi_elenco: {e})")
        return []

def messaggi_da_leggere(per_titolare, utente=None):
    """Quanti messaggi non letti. per_titolare=True conta quelli scritti dalle
    telefoniste (li deve leggere il titolare); False quelli scritti dal
    titolare per quella telefonista."""
    if not USE_DB:
        return {} if per_titolare else 0
    try:
        _msg_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            if per_titolare:
                cur.execute("SELECT utente, COUNT(*) FROM crm_messaggi "
                            "WHERE NOT da_titolare AND NOT letto GROUP BY utente")
                return {a: int(b) for a, b in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM crm_messaggi "
                        "WHERE utente=%s AND da_titolare AND NOT letto", (str(utente or '')[:80],))
            return int(cur.fetchone()[0])
    except Exception as e:
        print(f"  (messaggi_da_leggere: {e})")
        return {} if per_titolare else 0

def messaggi_segna_letti(utente, da_titolare):
    """Segna letti i messaggi della conversazione scritti dall'altra parte."""
    if not USE_DB:
        return False
    try:
        _msg_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("UPDATE crm_messaggi SET letto=true WHERE utente=%s "
                        "AND da_titolare=%s AND NOT letto",
                        (str(utente or '')[:80], bool(da_titolare)))
        return True
    except Exception as e:
        print(f"  (messaggi_segna_letti: {e})")
        return False


# ═══════════════════════════════════════════════════════════════════
#  ISCRIZIONI ALLE NOTIFICHE (telefono e computer)
# ═══════════════════════════════════════════════════════════════════
# Due tabelle piccole:
#   crm_push_chiavi = la coppia di chiavi VAPID, generata dal server alla
#                     prima notifica. Cosi' non c'e' niente da mettere su
#                     Railway e nessuna chiave segreta passa di mano.
#   crm_push        = un indirizzo di iscrizione per ogni browser/telefono.
#                     Un utente puo' averne piu' di uno (telefono + computer).
def _push_db_init():
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS crm_push_chiavi ("
                    "id INT PRIMARY KEY, privata TEXT NOT NULL, pubblica TEXT NOT NULL, "
                    "creato TIMESTAMPTZ DEFAULT now())")
        cur.execute("CREATE TABLE IF NOT EXISTS crm_push ("
                    "id BIGSERIAL PRIMARY KEY, utente TEXT NOT NULL, "
                    "endpoint TEXT NOT NULL UNIQUE, p256dh TEXT, auth TEXT, "
                    "dispositivo TEXT DEFAULT '', quando TIMESTAMPTZ DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS crm_push_utente ON crm_push (utente)")

def push_chiavi(crea_se_manca=True):
    """La coppia di chiavi VAPID. La genera al primo uso e la conserva."""
    if not USE_DB:
        return None, None
    try:
        _push_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT privata, pubblica FROM crm_push_chiavi WHERE id=1")
            r = cur.fetchone()
            if r:
                return r[0], r[1]
            if not crea_se_manca:
                return None, None
            import crm_push
            priv, pub = crm_push.genera_chiavi()
            if not priv:
                return None, None
            cur.execute("INSERT INTO crm_push_chiavi (id, privata, pubblica) VALUES (1,%s,%s) "
                        "ON CONFLICT (id) DO NOTHING", (priv, pub))
            cur.execute("SELECT privata, pubblica FROM crm_push_chiavi WHERE id=1")
            r = cur.fetchone()
            return (r[0], r[1]) if r else (None, None)
    except Exception as e:
        print(f"  (push_chiavi: {e})")
        return None, None

def push_iscrivi(utente, endpoint, p256dh='', auth='', dispositivo=''):
    if not USE_DB or not endpoint:
        return False
    try:
        _push_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO crm_push (utente, endpoint, p256dh, auth, dispositivo) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (endpoint) DO UPDATE SET "
                        "utente=EXCLUDED.utente, p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth, "
                        "dispositivo=EXCLUDED.dispositivo, quando=now()",
                        (str(utente or '')[:80], str(endpoint)[:900],
                         str(p256dh or '')[:200], str(auth or '')[:120],
                         str(dispositivo or '')[:120]))
        return True
    except Exception as e:
        print(f"  (push_iscrivi: {e})")
        return False

def push_disiscrivi(endpoint):
    if not USE_DB or not endpoint:
        return False
    try:
        _push_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_push WHERE endpoint=%s", (str(endpoint)[:900],))
        return True
    except Exception as e:
        print(f"  (push_disiscrivi: {e})")
        return False

def push_indirizzi(utente):
    """Gli indirizzi di iscrizione di un utente (telefono, computer...)."""
    if not USE_DB:
        return []
    try:
        _push_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT endpoint FROM crm_push WHERE utente=%s", (str(utente or '')[:80],))
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        print(f"  (push_indirizzi: {e})")
        return []

def push_quanti():
    """Quante iscrizioni per utente: serve alla diagnostica."""
    if not USE_DB:
        return {}
    try:
        _push_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT utente, COUNT(*) FROM crm_push GROUP BY utente")
            return {a: int(b) for a, b in cur.fetchall()}
    except Exception as e:
        print(f"  (push_quanti: {e})")
        return {}


def ui_tutti():
    """Stato interfaccia di TUTTI gli utenti: serve al titolare per vedere su
    quale scheda sta lavorando ciascuna telefonista in questo momento.
    La chiave e' il nome in minuscolo (come la salva ui_set)."""
    if not USE_DB:
        return {}
    try:
        _ui_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            cur.execute("SELECT utente, dati, aggiornato FROM crm_ui")
            fuori = {}
            for utente, dati, agg in cur.fetchall():
                try:
                    d = json.loads(dati) if dati else {}
                except Exception:
                    d = {}
                if not isinstance(d, dict):
                    d = {}
                d['aggiornato'] = agg.isoformat(timespec='seconds') if agg else ''
                fuori[utente] = d
            return fuori
    except Exception as e:
        print(f"  (ui_tutti: {e})")
        return {}


def messaggio_ultimo_non_letto(per_titolare, utente=None):
    """L'ultimo messaggio non letto, per l'anteprima nella notifica.
    per_titolare=True: l'ultimo scritto da una telefonista (lo deve leggere il
    titolare); False: l'ultimo scritto dal titolare per quella telefonista."""
    if not USE_DB:
        return None
    try:
        _msg_db_init()
        conn = _get_pg()
        with conn.cursor() as cur:
            if per_titolare:
                cur.execute("SELECT autore, testo, utente, id_contatto FROM crm_messaggi "
                            "WHERE NOT da_titolare AND NOT letto ORDER BY id DESC LIMIT 1")
            else:
                cur.execute("SELECT autore, testo, utente, id_contatto FROM crm_messaggi "
                            "WHERE utente=%s AND da_titolare AND NOT letto "
                            "ORDER BY id DESC LIMIT 1", (str(utente or '')[:80],))
            r = cur.fetchone()
        if not r:
            return None
        return {'autore': r[0] or '', 'testo': (r[1] or '')[:140],
                'utente': r[2] or '', 'id_contatto': r[3] or ''}
    except Exception as e:
        print(f"  (messaggio_ultimo_non_letto: {e})")
        return None
