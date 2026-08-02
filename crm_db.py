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
