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
MAX_BACKUPS = 60

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_DB = bool(DATABASE_URL)

# ─────────────────────────────────────────────────────────────
#  MODO ONLINE — PostgreSQL (Railway)
# ─────────────────────────────────────────────────────────────
_pg = None
def _get_pg():
    """Connessione PostgreSQL (psycopg 3). I dati del CRM stanno in
    un'unica riga JSON nella tabella crm_blob: semplice e robusto,
    e mantiene identica la struttura dati che il programma già usa."""
    global _pg
    import psycopg
    url = DATABASE_URL
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    if _pg is None or _pg.closed:
        _pg = psycopg.connect(url, autocommit=True)
    return _pg

def _db_init():
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS crm_blob (id INT PRIMARY KEY, data JSONB)")
        # tabella per i backup giornalieri (uno per giorno)
        cur.execute("CREATE TABLE IF NOT EXISTS crm_backup (giorno TEXT PRIMARY KEY, data JSONB, creato TIMESTAMP DEFAULT now())")

def _db_load():
    _db_init()
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM crm_blob WHERE id=1")
        row = cur.fetchone()
        if row and row[0]:
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return {}

_last_db_backup_day = None
def _db_save(data):
    global _last_db_backup_day
    _db_init()
    conn = _get_pg()
    payload = json.dumps(data, ensure_ascii=False)
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
    try:
        d = _db_load()
        return bool(d.get('contacts'))
    except Exception:
        return False

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

def save_data(data):
    return _db_save(data) if USE_DB else _file_save(data)

def has_data():
    return _db_has_data() if USE_DB else DATA_FILE.exists()

def modo():
    return 'DATABASE PostgreSQL (online)' if USE_DB else 'file locale crm_data.json'

# Caricamento iniziale dei dati nel database, da file, una sola volta.
# Si usa al primo avvio online: se il DB è vuoto e c'è il file, lo importa.
def seed_from_file_if_empty():
    if not USE_DB:
        return False
    try:
        if _db_has_data():
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
    except Exception as e:
        print(f"  (primo caricamento non riuscito: {e})")
    return False
