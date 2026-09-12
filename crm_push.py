#!/usr/bin/env python3
"""
crm_push.py — Notifiche sul telefono e sul computer, anche col CRM CHIUSO.

Come funziona, in breve:
  1. il browser (o l'app installata su Android) chiede al CRM la chiave
     pubblica e si iscrive al servizio di notifiche del suo browser;
  2. il CRM salva l'indirizzo dell'iscrizione nel database;
  3. quando arriva un messaggio, il server manda una "sveglia" a quell'indirizzo;
  4. il service worker (sw.js) si sveglia, chiede al CRM quanti messaggi ci
     sono e mostra la notifica.

DUE SCELTE IMPORTANTI

1. Le chiavi VAPID le genera il SERVER da solo, alla prima notifica, e le
   tiene nel database. Non c'e' niente da mettere su Railway e nessuna chiave
   segreta deve passare di mano. Se un giorno il database venisse rifatto, le
   chiavi cambiano e basta riscriversi alle notifiche.

2. La sveglia viene mandata SENZA contenuto. Mandare il testo del messaggio
   dentro la notifica richiederebbe di cifrarlo con uno schema complicato
   (aes128gcm + ECDH): piu' codice, piu' cose che si rompono. Cosi' invece il
   testo non viaggia mai fuori dal CRM, ed e' anche piu' riservato: la
   notifica dice "hai un messaggio nuovo", e il resto si legge entrando.

Solo 'cryptography' come libreria in piu' (serve per la firma ES256), e
urllib della libreria standard per la chiamata.
"""
import base64
import json
import time
import urllib.request
import urllib.error

# La libreria potrebbe non essere installata: in quel caso le notifiche sono
# semplicemente spente e il CRM funziona come prima. NON deve mai impedire
# l'avvio del programma.
try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    CRIPTO_OK = True
except Exception as _e:            # pragma: no cover
    CRIPTO_OK = False
    _ERRORE_CRIPTO = str(_e)

SCADENZA_JWT = 12 * 3600           # la firma vale 12 ore
TIMEOUT_INVIO = 8                  # secondi per ogni notifica


def _b64(dati: bytes) -> str:
    """base64 senza '=' finali, come vuole il protocollo delle notifiche."""
    return base64.urlsafe_b64encode(dati).decode('ascii').rstrip('=')


def _b64_dec(testo: str) -> bytes:
    testo = (testo or '').strip()
    return base64.urlsafe_b64decode(testo + '=' * (-len(testo) % 4))


def genera_chiavi():
    """Nuova coppia di chiavi VAPID. Ritorna (privata_b64, pubblica_b64).
    La pubblica e' nel formato non compresso di 65 byte che vuole il browser."""
    if not CRIPTO_OK:
        return None, None
    chiave = ec.generate_private_key(ec.SECP256R1())
    privata = chiave.private_numbers().private_value.to_bytes(32, 'big')
    pubblica = chiave.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    return _b64(privata), _b64(pubblica)


def _carica_privata(privata_b64):
    valore = int.from_bytes(_b64_dec(privata_b64), 'big')
    return ec.derive_private_key(valore, ec.SECP256R1())


def _firma_jwt(origine, privata_b64, contatto='mailto:info@collezioni-istituzionali.it'):
    """JWT ES256 per il servizio di notifiche del browser.
    'origine' e' lo schema+host dell'indirizzo dell'iscrizione."""
    testata = _b64(json.dumps({'typ': 'JWT', 'alg': 'ES256'},
                              separators=(',', ':')).encode())
    corpo = _b64(json.dumps({'aud': origine,
                             'exp': int(time.time()) + SCADENZA_JWT,
                             'sub': contatto},
                            separators=(',', ':')).encode())
    da_firmare = f'{testata}.{corpo}'.encode('ascii')
    chiave = _carica_privata(privata_b64)
    # cryptography firma in formato DER: il protocollo vuole i due numeri
    # r ed s attaccati, 32 byte ciascuno.
    der = chiave.sign(da_firmare, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    firma = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
    return f'{testata}.{corpo}.{_b64(firma)}'


def _origine(endpoint):
    from urllib.parse import urlparse
    p = urlparse(endpoint)
    return f'{p.scheme}://{p.netloc}'


def invia_sveglia(endpoint, privata_b64, pubblica_b64, urgenza='normal', ttl=86400):
    """Manda la sveglia a UN indirizzo. Ritorna (riuscito, codice, nota).

    codice 404 o 410 = iscrizione non piu' valida: chi chiama la deve
    cancellare dal database, altrimenti resta a provare per sempre.
    """
    if not CRIPTO_OK:
        return False, 0, 'libreria cryptography non disponibile'
    try:
        jwt = _firma_jwt(_origine(endpoint), privata_b64)
        richiesta = urllib.request.Request(endpoint, data=b'', method='POST')
        richiesta.add_header('Authorization', f'vapid t={jwt}, k={pubblica_b64}')
        richiesta.add_header('TTL', str(int(ttl)))
        richiesta.add_header('Urgency', urgenza)
        richiesta.add_header('Content-Length', '0')
        with urllib.request.urlopen(richiesta, timeout=TIMEOUT_INVIO) as risposta:
            return True, risposta.status, ''
    except urllib.error.HTTPError as e:
        return False, e.code, (e.reason or '')
    except Exception as e:
        return False, 0, str(e)


def stato():
    """Serve alla diagnostica: dice se le notifiche possono funzionare."""
    if not CRIPTO_OK:
        return {'pronto': False, 'motivo': globals().get('_ERRORE_CRIPTO', 'cryptography assente')}
    return {'pronto': True, 'motivo': ''}
