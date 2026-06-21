#!/usr/bin/env python3
"""
crm_auth.py — Accesso con password e ruoli (titolare / operatore).

Attivo SOLO online (quando c'è DATABASE_URL). In locale sul Mac il CRM
resta aperto come prima, senza login, perché lo usi solo tu.

Ruoli:
  • titolare  → accesso completo (tutto).
  • operatore → può cercare, chiamare, prendere note, fissare appuntamenti,
                stampare/inviare la SINGOLA scheda. NON può: eliminare,
                resettare, importare CSV, esportare/stampare in massa.

Gli utenti e le password si impostano con variabili d'ambiente su Railway,
così le password NON stanno scritte nel codice. Esempio di variabile:
  CRM_UTENTI = alessandro:LaMiaPassword:titolare, anna:Pwd1:operatore, ...
Se non impostata, vale un accesso titolare di prova (da cambiare subito).
"""
import os, hashlib, hmac, secrets, time, json, base64

USE_AUTH = bool(os.environ.get('DATABASE_URL', '').strip())
SECRET = os.environ.get('CRM_SECRET', '').strip() or secrets.token_hex(16)

# numero massimo di accessi previsti (1 titolare + 4 operatori)
MAX_UTENTI = 5

# ── ZONE: una zona è un insieme di regioni. Configurabile via CRM_ZONE. ──
# Formato CRM_ZONE: nomezona=Regione1|Regione2|...; altrazona=Regione3|...
# Esempio: centro=Lazio|Umbria|Marche|Sardegna
# Se non impostata, vale la mappa predefinita qui sotto.
ZONE_DEFAULT = {
    'centro': ['Lazio', 'Umbria', 'Marche', 'Sardegna'],
    'nord': ['Piemonte', "Valle d'Aosta", 'Lombardia', 'Veneto', 'Trentino-Alto Adige', 'Friuli-Venezia Giulia', 'Liguria'],
}
def _parse_zone():
    raw = os.environ.get('CRM_ZONE', '').strip()
    zone = {}
    if raw:
        for blocco in raw.split(';'):
            if '=' in blocco:
                nome, regs = blocco.split('=', 1)
                nome = nome.strip().lower()
                lista = [r.strip() for r in regs.split('|') if r.strip()]
                if nome and lista:
                    zone[nome] = lista
    if not zone:
        zone = dict(ZONE_DEFAULT)
    return zone

ZONE = _parse_zone()

def regioni_della_zona(zona):
    """Ritorna la lista di regioni per una zona, o None se la zona non esiste/è vuota (= nessun filtro)."""
    if not zona:
        return None
    return ZONE.get(str(zona).lower())

def _parse_utenti():
    """Legge gli utenti dalla variabile CRM_UTENTI.
    Formato: nome:password:ruolo[:zona] separati da virgola.
    Esempio: alessandro:Segreta1:titolare, centro1:Pwd:operatore:centro"""
    raw = os.environ.get('CRM_UTENTI', '').strip()
    utenti = {}
    if raw:
        for pezzo in raw.split(','):
            parti = pezzo.strip().split(':')
            if len(parti) >= 2:
                nome = parti[0].strip()
                pwd = parti[1].strip()
                ruolo = (parti[2].strip() if len(parti) > 2 else 'operatore').lower()
                if ruolo not in ('titolare', 'operatore'):
                    ruolo = 'operatore'
                zona = (parti[3].strip().lower() if len(parti) > 3 else '')
                if nome and pwd:
                    utenti[nome.lower()] = {'password': pwd, 'ruolo': ruolo, 'nome': nome, 'zona': zona}
    if not utenti:
        # accesso di prova: SOLO se non è stato configurato nulla. Da cambiare subito.
        utenti['admin'] = {'password': 'cambiami', 'ruolo': 'titolare', 'nome': 'admin', 'zona': ''}
    return utenti

UTENTI = _parse_utenti()

# ── token di sessione firmati (niente database necessario) ──
def _firma(msg):
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def crea_token(nome, ruolo, ore=12, zona=''):
    scad = int(time.time()) + ore*3600
    corpo = f"{nome}|{ruolo}|{zona}|{scad}"
    firma = _firma(corpo)
    return base64.urlsafe_b64encode(f"{corpo}|{firma}".encode()).decode()

def verifica_token(token):
    try:
        dati = base64.urlsafe_b64decode(token.encode()).decode()
        nome, ruolo, zona, scad, firma = dati.rsplit('|', 4)
        if _firma(f"{nome}|{ruolo}|{zona}|{scad}") != firma:
            return None
        if int(scad) < int(time.time()):
            return None
        return {'nome': nome, 'ruolo': ruolo, 'zona': zona}
    except Exception:
        return None

def controlla_login(nome, password):
    u = UTENTI.get((nome or '').lower().strip())
    if not u:
        return None
    # confronto a tempo costante (anti-indovinare)
    if hmac.compare_digest(u['password'], password or ''):
        return {'nome': u['nome'], 'ruolo': u['ruolo'], 'zona': u.get('zona', '')}
    return None

# ── blocco dopo troppi tentativi sbagliati ──
MAX_TENTATIVI = 3          # tentativi consentiti prima del blocco
BLOCCO_MINUTI = 5          # durata del blocco in minuti
_tentativi = {}            # {nome: [numero_falliti, timestamp_blocco]}

def stato_blocco(nome):
    """Restituisce i secondi di blocco rimanenti per questo utente (0 se libero)."""
    k = (nome or '').lower().strip()
    rec = _tentativi.get(k)
    if not rec:
        return 0
    falliti, bloccato_fino = rec
    if bloccato_fino and bloccato_fino > time.time():
        return int(bloccato_fino - time.time())
    return 0

def registra_tentativo(nome, ok):
    """Aggiorna il contatore tentativi. Se ok=True azzera, altrimenti incrementa e blocca."""
    k = (nome or '').lower().strip()
    if ok:
        _tentativi.pop(k, None)
        return
    rec = _tentativi.get(k, [0, 0])
    rec[0] += 1
    if rec[0] >= MAX_TENTATIVI:
        rec[1] = time.time() + BLOCCO_MINUTI * 60   # blocca
        rec[0] = 0                                  # azzera il contatore dopo il blocco
    _tentativi[k] = rec

# ── azioni riservate al solo TITOLARE ──
AZIONI_SOLO_TITOLARE = {
    'reset',            # azzerare il database
    'import_csv',       # caricare CSV (sovrascrive)
    'elimina',          # eliminare un contatto
    'export_massa',     # esportare/stampare elenchi di massa
    'unisci',           # unire schede (potenziale perdita)
    'gestione_utenti',  # creare/cambiare utenti
}

# soglia: stampare/inviare email per più di questi contatti = azione di massa
LIMITE_STAMPA_MASSA = 15

def puo_fare(ruolo, azione):
    if ruolo == 'titolare':
        return True
    return azione not in AZIONI_SOLO_TITOLARE
