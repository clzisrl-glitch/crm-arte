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
# Esempio: centro=Lazio|Umbria|Marche|Sardegna|Sicilia|Campania
# Se non impostata, vale la mappa predefinita qui sotto.
ZONE_DEFAULT = {
    'centro': ['Lazio', 'Umbria', 'Marche', 'Sardegna', 'Sicilia', 'Campania', 'Calabria'],
    # 11/09/2026: nasce la zona TRIVENETO (Veneto + Trentino-Alto Adige +
    # Friuli-Venezia Giulia), staccata dal Nord. Le zone NON si sovrappongono:
    # se una regione stesse in due zone, due operatori si troverebbero gli
    # stessi contatti e le stesse agende. Quindi il Nord resta con Piemonte,
    # Valle d'Aosta, Lombardia e Liguria.
    'nord': ['Piemonte', "Valle d'Aosta", 'Lombardia', 'Liguria'],
    'triveneto': ['Veneto', 'Trentino-Alto Adige', 'Friuli-Venezia Giulia'],
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
#
# NESSUNA SCADENZA A ORE (25/08/2026). Prima il token aveva una scadenza
# incorporata (12 ore in origine, poi accorciata per errore a 2 ore l'11/08,
# poi allungata a 12 ore e a 30 giorni per correggere il bug "CRM non
# salva" - vedi rapporto, appendice 3). Il titolare ha chiesto di non avere
# NESSUN numero di ore: qualunque limite, per quanto lungo, resta un
# limite. Tolto del tutto: un token con firma valida resta valido a
# oltranza, punto.
# La sicurezza non sparisce: resta la login vera e propria (password),
# resta il cookie "di sessione" (crm_server.py: nessun max_age, sparisce da
# solo alla chiusura del browser - quello e' il logout che conta) e resta
# il pulsante Esci (/api/logout) per un logout esplicito in qualsiasi
# momento. Per invalidare TUTTI i login in un colpo solo, in un'emergenza,
# resta la leva di sempre: cambiare CRM_SECRET su Railway (sezione 7 del
# rapporto: farlo SOLO se necessario, disconnette anche tutti gli operatori).
def _firma(msg):
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def crea_token(nome, ruolo, zona=''):
    # Il campo "creato" resta nel token solo a scopo informativo (non e'
    # piu' controllato da verifica_token): utile per un domani, se servisse
    # di nuovo un limite, senza dover cambiare il formato del token.
    creato = int(time.time())
    corpo = f"{nome}|{ruolo}|{zona}|{creato}"
    firma = _firma(corpo)
    return base64.urlsafe_b64encode(f"{corpo}|{firma}".encode()).decode()

def verifica_token(token):
    try:
        dati = base64.urlsafe_b64decode(token.encode()).decode()
        nome, ruolo, zona, creato, firma = dati.rsplit('|', 4)
        if _firma(f"{nome}|{ruolo}|{zona}|{creato}") != firma:
            return None
        return {'nome': nome, 'ruolo': ruolo, 'zona': zona}
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════
#  PASSWORD CIFRATE (impronta, non password)
# ══════════════════════════════════════════════════════════════════
# Del password non si salva MAI il testo: si salva un'impronta calcolata
# con PBKDF2-SHA256 e 200.000 giri, piu' un "sale" casuale diverso per
# ogni utente. Dall'impronta non si torna indietro alla password, e due
# persone con la stessa password hanno impronte diverse.
# Formato salvato:  pbkdf2$<giri>$<sale_hex>$<impronta_hex>
# Solo libreria standard: niente pacchetti nuovi da installare.
PBKDF2_GIRI = 200000

def crea_impronta(password, giri=PBKDF2_GIRI):
    sale = secrets.token_bytes(16)
    imp = hashlib.pbkdf2_hmac('sha256', (password or '').encode('utf-8'), sale, giri)
    return f"pbkdf2${giri}${sale.hex()}${imp.hex()}"

def verifica_impronta(password, impronta):
    """True se la password corrisponde all'impronta salvata."""
    try:
        algo, giri, sale_hex, atteso_hex = str(impronta).split('$')
        if algo != 'pbkdf2':
            return False
        imp = hashlib.pbkdf2_hmac('sha256', (password or '').encode('utf-8'),
                                  bytes.fromhex(sale_hex), int(giri))
        return hmac.compare_digest(imp.hex(), atteso_hex)
    except Exception:
        return False

# Gli utenti gestiti dentro il CRM (tabella cifrata) vengono caricati qui
# dal server all'avvio e a ogni modifica. Formato:
#   {'nord1': {'nome':'Nord1','impronta':'pbkdf2$...','ruolo':'operatore','zona':'nord'}}
UTENTI_DB = {}

def imposta_utenti_db(elenco):
    """Sostituisce l'elenco degli utenti cifrati (lo chiama crm_server)."""
    global UTENTI_DB
    nuovo = {}
    for u in (elenco or []):
        nome = str(u.get('nome', '')).strip()
        if not nome:
            continue
        nuovo[nome.lower()] = {
            'nome': nome,
            'impronta': u.get('impronta', ''),
            'ruolo': (u.get('ruolo') or 'operatore').lower(),
            'zona': (u.get('zona') or '').lower(),
        }
    UTENTI_DB = nuovo
    return len(UTENTI_DB)

def controlla_login(nome, password):
    """Prima gli utenti gestiti dal CRM (password cifrata), poi la variabile
    CRM_UTENTI (password in chiaro) come chiave di riserva dell'admin.
    Chi e' nella tabella cifrata NON viene piu' cercato nella variabile:
    la password valida e' quella impostata dentro il CRM."""
    chiave = (nome or '').lower().strip()
    u = UTENTI_DB.get(chiave)
    if u:
        if verifica_impronta(password, u.get('impronta', '')):
            return {'nome': u['nome'], 'ruolo': u['ruolo'], 'zona': u.get('zona', '')}
        return None
    v = UTENTI.get(chiave)
    if not v:
        return None
    # L'accesso di prova admin/cambiami esiste solo per il primissimo avvio.
    # Se il CRM ha gia' utenti veri con password cifrata, quell'accesso NON
    # deve piu' funzionare: altrimenti svuotare CRM_UTENTI aprirebbe la porta
    # a chiunque conosca il valore predefinito.
    if UTENTI_DB and chiave == 'admin' and v.get('password') == 'cambiami':
        return None
    # confronto a tempo costante (anti-indovinare)
    if hmac.compare_digest(v['password'], password or ''):
        return {'nome': v['nome'], 'ruolo': v['ruolo'], 'zona': v.get('zona', '')}
    return None

def nomi_titolari():
    """Nomi di TUTTI gli account titolare, cifrati e da variabile. Serve al
    frontend per nascondere dall'agenda delle telefoniste le telefonate e gli
    appuntamenti registrati dal titolare. Prima si leggeva solo la variabile
    CRM_UTENTI: spostando Admin fra le password cifrate era sparito da questa
    lista e le sue telefonate ricomparivano nelle agende di zona."""
    nomi = set()
    for u in UTENTI_DB.values():
        if u.get('ruolo') == 'titolare':
            nomi.add(u['nome'])
    for k, v in UTENTI.items():
        if v.get('ruolo') == 'titolare' and k not in UTENTI_DB:
            nomi.add(v['nome'])
    return sorted(nomi)

def elenco_utenti_visibile():
    """Nomi, ruoli e zone di TUTTI gli utenti (cifrati + variabile), senza
    nessuna password. Serve alla scheda Utenti."""
    fuori = []
    for k, u in sorted(UTENTI_DB.items()):
        fuori.append({'nome': u['nome'], 'ruolo': u['ruolo'], 'zona': u.get('zona', ''),
                      'origine': 'crm'})
    for k, v in sorted(UTENTI.items()):
        if k in UTENTI_DB:
            continue
        fuori.append({'nome': v['nome'], 'ruolo': v['ruolo'], 'zona': v.get('zona', ''),
                      'origine': 'variabile'})
    return fuori

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


# ── ORARIO DI LAVORO DEGLI OPERATORI ────────────────────────────────────────
# Le telefoniste possono usare il CRM solo dentro questa fascia; il titolare
# non ha limiti. Le ore sono quelle ITALIANE, calcolate qui e non prese dal
# fuso del server (Railway lavora in UTC: sarebbero 2 ore indietro d'estate e
# 1 d'inverno). Modificabili da Railway con le variabili CRM_ORA_APERTURA e
# CRM_ORA_CHIUSURA, senza toccare il codice.
ORA_APERTURA = int(os.environ.get('CRM_ORA_APERTURA', '8'))
ORA_CHIUSURA = int(os.environ.get('CRM_ORA_CHIUSURA', '21'))

def _ora_italiana(adesso=None):
    """Ora locale italiana. Usa i dati dei fusi se il sistema li ha, altrimenti
    applica a mano la regola europea: ora legale dall'ultima domenica di marzo
    all'ultima domenica di ottobre (cambio alle 01:00 UTC)."""
    import datetime as _dt
    t = adesso or _dt.datetime.now(_dt.timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo('Europe/Rome'))
    except Exception:
        pass
    def _ultima_domenica(anno, mese):
        d = _dt.datetime(anno, mese, 31, 1, tzinfo=_dt.timezone.utc)
        while d.weekday() != 6:          # 6 = domenica
            d -= _dt.timedelta(days=1)
        return d
    legale = _ultima_domenica(t.year, 3) <= t < _ultima_domenica(t.year, 10)
    return t + _dt.timedelta(hours=2 if legale else 1)

def fuori_orario(ruolo, adesso=None):
    """True se un operatore sta usando il CRM fuori dall'orario di lavoro."""
    if ruolo == 'titolare':
        return False
    return not (ORA_APERTURA <= _ora_italiana(adesso).hour < ORA_CHIUSURA)

def messaggio_fuori_orario(adesso=None):
    return ('Il CRM e aperto dalle %02d:00 alle %02d:00. In Italia adesso sono '
            'le %s: riprova durante l\'orario di lavoro.'
            % (ORA_APERTURA, ORA_CHIUSURA, _ora_italiana(adesso).strftime('%H:%M')))
