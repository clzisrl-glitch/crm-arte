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

def _parse_utenti():
    """Legge gli utenti dalla variabile CRM_UTENTI.
    Formato: nome:password:ruolo separati da virgola.
    Esempio: alessandro:Segreta1:titolare, anna:Pwd:operatore"""
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
                if nome and pwd:
                    utenti[nome.lower()] = {'password': pwd, 'ruolo': ruolo, 'nome': nome}
    if not utenti:
        # accesso di prova: SOLO se non è stato configurato nulla. Da cambiare subito.
        utenti['admin'] = {'password': 'cambiami', 'ruolo': 'titolare', 'nome': 'admin'}
    return utenti

UTENTI = _parse_utenti()

# ── token di sessione firmati (niente database necessario) ──
def _firma(msg):
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def crea_token(nome, ruolo, ore=12):
    scad = int(time.time()) + ore*3600
    corpo = f"{nome}|{ruolo}|{scad}"
    firma = _firma(corpo)
    return base64.urlsafe_b64encode(f"{corpo}|{firma}".encode()).decode()

def verifica_token(token):
    try:
        dati = base64.urlsafe_b64decode(token.encode()).decode()
        nome, ruolo, scad, firma = dati.rsplit('|', 3)
        if _firma(f"{nome}|{ruolo}|{scad}") != firma:
            return None
        if int(scad) < int(time.time()):
            return None
        return {'nome': nome, 'ruolo': ruolo}
    except Exception:
        return None

def controlla_login(nome, password):
    u = UTENTI.get((nome or '').lower().strip())
    if not u:
        return None
    # confronto a tempo costante (anti-indovinare)
    if hmac.compare_digest(u['password'], password or ''):
        return {'nome': u['nome'], 'ruolo': u['ruolo']}
    return None

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
