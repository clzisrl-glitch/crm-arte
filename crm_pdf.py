# -*- coding: utf-8 -*-
"""
Generatore PDF per Arte Editoria Monete.
Ricostruisce il layout "scheda" (V6) con reportlab: niente browser,
funziona identico in locale (Mac) e online (Railway / Linux).
Una scheda per pagina; piu' schede = piu' pagine.
"""
import re, io

from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                Spacer, PageBreak, KeepTogether)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT

BLU = colors.HexColor('#1a3a6a')
BLU_CHIARO = colors.HexColor('#1a6aa0')
GRIGIO = colors.HexColor('#8a96a6')
RIGA = colors.HexColor('#dfe5ee')
ZEBRA = colors.HexColor('#f5f8fc')
TOT_BG = colors.HexColor('#eef2fa')
TESTO = colors.HexColor('#16202e')


def _esc(s):
    s = '' if s is None else str(s)
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _fd(s):
    """Data pulita gg/mm/aaaa, togliendo eventuale orario."""
    s = '' if s is None else str(s).strip()
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f'{m.group(3)}/{m.group(2)}/{m.group(1)}'
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if m:
        return f'{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}'
    return s


def _ft(s):
    """Orario HH:MM da una stringa che lo contiene."""
    s = '' if s is None else str(s)
    m = re.search(r'\b(\d{1,2}):(\d{2})', s)
    return f'{int(m.group(1)):02d}:{m.group(2)}' if m else ''


def _money(v):
    n = re.sub(r'[^\d,.-]', '', str(v or '')).replace(',', '.')
    try:
        f = float(n)
    except ValueError:
        return str(v or '')
    if f == 0:
        return ''
    return '\u20ac ' + format(int(round(f)), ',d').replace(',', '.')


def _styles():
    ss = getSampleStyleSheet()
    out = {}
    out['name'] = ParagraphStyle('name', parent=ss['Normal'], fontName='Helvetica-Bold',
                                 fontSize=16, textColor=TESTO, leading=18)
    out['sub'] = ParagraphStyle('sub', parent=ss['Normal'], fontName='Helvetica-Oblique',
                                fontSize=8.5, textColor=colors.HexColor('#5a6676'), leading=11)
    out['hid'] = ParagraphStyle('hid', parent=ss['Normal'], fontName='Helvetica',
                                fontSize=8, textColor=GRIGIO, alignment=TA_RIGHT, leading=10)
    out['sec'] = ParagraphStyle('sec', parent=ss['Normal'], fontName='Helvetica-Bold',
                                fontSize=8, textColor=BLU, leading=11, spaceBefore=8, spaceAfter=3)
    out['lbl'] = ParagraphStyle('lbl', parent=ss['Normal'], fontName='Helvetica',
                                fontSize=7.5, textColor=GRIGIO, leading=10)
    out['val'] = ParagraphStyle('val', parent=ss['Normal'], fontName='Helvetica',
                                fontSize=9, textColor=TESTO, leading=11)
    out['valb'] = ParagraphStyle('valb', parent=ss['Normal'], fontName='Helvetica-Bold',
                                 fontSize=9, textColor=TESTO, leading=11)
    out['big'] = ParagraphStyle('big', parent=ss['Normal'], fontName='Helvetica-Bold',
                                fontSize=14, textColor=TESTO, leading=16)
    out['note'] = ParagraphStyle('note', parent=ss['Normal'], fontName='Helvetica',
                                 fontSize=8.5, textColor=TESTO, leading=12)
    out['cell'] = ParagraphStyle('cell', parent=ss['Normal'], fontName='Helvetica',
                                 fontSize=8, textColor=TESTO, leading=10)
    out['cellb'] = ParagraphStyle('cellb', parent=ss['Normal'], fontName='Helvetica-Bold',
                                  fontSize=8, textColor=TESTO, leading=10)
    out['th'] = ParagraphStyle('th', parent=ss['Normal'], fontName='Helvetica-Bold',
                               fontSize=7, textColor=BLU, leading=9)
    out['thr'] = ParagraphStyle('thr', parent=ss['Normal'], fontName='Helvetica-Bold',
                                fontSize=7, textColor=BLU, leading=9, alignment=TA_RIGHT)
    out['cellr'] = ParagraphStyle('cellr', parent=ss['Normal'], fontName='Helvetica',
                                  fontSize=8, textColor=TESTO, leading=10, alignment=TA_RIGHT)
    out['totr'] = ParagraphStyle('totr', parent=ss['Normal'], fontName='Helvetica-Bold',
                                 fontSize=8, textColor=BLU, leading=10, alignment=TA_RIGHT)
    return out


def _left_col(c, st):
    """Colonna sinistra: recapiti, dati, note."""
    el = []
    nums = [('Cellulare', c.get('Cellulare')), ('Cellulare 2', c.get('Cellulare2')),
            ('Telefono', c.get('Telefono')), ('Telefono 2', c.get('Telefono2')),
            ('Tel. ufficio', c.get('Tel_Ufficio'))]
    nums = [(l, v) for l, v in nums if str(v or '').strip()]
    if nums:
        el.append(Paragraph(_esc(nums[0][1]) + '  <font size=7 color="#8a96a6">' +
                            nums[0][0].upper() + '</font>', st['big']))
        for l, v in nums[1:]:
            el.append(_kv(l, v, st))
    if c.get('Email'):
        el.append(_kv('e-mail', c['Email'], st))
    nasc = ' a '.join([x for x in [_fd(c.get('Data_nascita')), c.get('Luogo_nascita')] if str(x or '').strip()])
    if nasc:
        el.append(_kv('Nascita', nasc, st))
    if c.get('Codice_fiscale'):
        el.append(_kv('Cod. fiscale', c['Codice_fiscale'], st))
    if c.get('Note_telefono'):
        el.append(_kv('Stato numero', c['Note_telefono'], st))
    addr = ', '.join([x for x in [
        c.get('Via'),
        ' '.join([y for y in [c.get('CAP'), c.get('Citta')] if str(y or '').strip()]),
        f"({c.get('Provincia')})" if str(c.get('Provincia') or '').strip() else '',
        c.get('Regione')] if str(x or '').strip()])
    if addr:
        el.append(_kv('Indirizzo', addr, st))
    if c.get('Note'):
        el.append(Paragraph('NOTE', st['sec']))
        el.append(Paragraph(_esc(c['Note']).replace('\n', '<br/>'), st['note']))
    if c.get('Valore_acquistato'):
        el.append(Spacer(1, 4))
        el.append(_kv('Valore acq.', _money(c['Valore_acquistato']), st, bold=True))
    return el


def _kv(label, value, st, bold=False):
    t = Table([[Paragraph(_esc(label), st['lbl']),
                Paragraph(_esc(value), st['valb'] if bold else st['val'])]],
              colWidths=[26 * mm, None])
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1.2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.2),
    ]))
    return t


def _right_col(c, opere, tels, st):
    """Colonna destra: ultima telefonata, opere, note appuntamento."""
    el = []
    # ultima telefonata (la piu' recente per data)
    ut = None
    if tels:
        def keyf(t):
            s = str(t.get('Data_da_fare') or '')
            m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', s) or re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
            if not m:
                return ''
            if '/' in s:
                return f'{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}'
            return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
        ut = sorted(tels, key=keyf, reverse=True)[0]
    if ut:
        el.append(Paragraph('ULTIMA TELEFONATA', st['sec']))
        txt = '<b>' + _esc(_fd(ut.get('Data_da_fare')))
        ora = _ft(ut.get('Data_da_fare'))
        if ora:
            txt += ' <font color="#8a96a6">' + ora + '</font>'
        txt += '</b>'
        if ut.get('Esito'):
            txt += ' \u00b7 ' + _esc(ut['Esito'])
        if c.get('Prossima_telefonata'):
            txt += ' \u2192 prossima ' + _esc(_fd(c['Prossima_telefonata']))
            if c.get('Ora_appuntamento'):
                txt += ' ore ' + _esc(_ft(c['Ora_appuntamento']))
        el.append(Paragraph(txt, st['val']))

    # opere acquistate
    if opere:
        hasD = any(str(o.get('Data_acquisto') or '').strip() for o in opere)
        hasE = any(str(o.get('Editore') or '').strip() for o in opere)
        hasA = any(str(o.get('Artista') or '').strip() for o in opere)
        hasP = any(str(o.get('Pagamento') or '').strip() for o in opere)
        hasR = any(str(o.get('Rate') or '').strip() for o in opere)
        el.append(Paragraph('OPERE ACQUISTATE (%d)' % len(opere), st['sec']))
        head = []
        cols = []
        if hasD: head.append(Paragraph('Data acq.', st['th'])); cols.append('D')
        if hasE: head.append(Paragraph('Editore', st['th'])); cols.append('E')
        if hasA: head.append(Paragraph('Artista', st['th'])); cols.append('A')
        head.append(Paragraph('Opera', st['th'])); cols.append('O')
        head.append(Paragraph('Valore', st['thr'])); cols.append('V')
        if hasP: head.append(Paragraph('Tipo', st['th'])); cols.append('P')
        if hasR: head.append(Paragraph('Rate', st['thr'])); cols.append('R')
        rows = [head]
        tot = 0.0
        for o in opere:
            r = []
            if hasD: r.append(Paragraph(_esc(_fd(o.get('Data_acquisto'))), st['cell']))
            if hasE: r.append(Paragraph(_esc(o.get('Editore') or ''), st['cell']))
            if hasA: r.append(Paragraph(_esc(o.get('Artista') or ''), st['cell']))
            r.append(Paragraph(_esc(o.get('Opera') or ''), st['cell']))
            r.append(Paragraph(_money(o.get('Valore')), st['cellr']))
            if hasP: r.append(Paragraph(_esc(o.get('Pagamento') or ''), st['cell']))
            if hasR: r.append(Paragraph(_esc(o.get('Rate') or ''), st['cellr']))
            rows.append(r)
            n = re.sub(r'[^\d,.-]', '', str(o.get('Valore') or '')).replace(',', '.')
            try: tot += float(n)
            except ValueError: pass
        # riga totale
        if tot:
            totrow = [''] * (len(cols) - 1) + [Paragraph('Totale ' + _money(str(int(round(tot)))), st['totr'])]
            # metti l'etichetta totale a sinistra unita
            rows.append([Paragraph('Totale', st['totr'])] + [''] * (len(cols) - 2) +
                        [Paragraph(_money(str(int(round(tot)))), st['totr'])])
        # larghezze colonne proporzionate
        widths = _col_widths(cols)
        t = Table(rows, colWidths=widths, repeatRows=1)
        style = [
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LINEBELOW', (0, 0), (-1, 0), 1.2, BLU),
            ('LINEBELOW', (0, 1), (-1, -2 if tot else -1), 0.4, RIGA),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('ROWBACKGROUNDS', (0, 1), (-1, -2 if tot else -1), [colors.white, ZEBRA]),
        ]
        if tot:
            style += [
                ('BACKGROUND', (0, -1), (-1, -1), TOT_BG),
                ('LINEABOVE', (0, -1), (-1, -1), 1.2, BLU),
                ('SPAN', (0, -1), (-2, -1)),
            ]
        t.setStyle(TableStyle(style))
        el.append(t)

    # note appuntamento (ultime 5)
    apps = [t for t in tels if str(t.get('Note_appuntamento') or '').strip()]
    def keyf2(t):
        s = str(t.get('Data_appuntamento') or t.get('Data_da_fare') or '')
        m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
        return f'{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}' if m else ''
    apps = sorted(apps, key=keyf2, reverse=True)[:5]
    if apps:
        el.append(Paragraph('NOTE APPUNTAMENTO (ultime %d)' % len(apps), st['sec']))
        rows = [[Paragraph('Data', st['th']), Paragraph('Ora', st['th']),
                 Paragraph('Esito', st['th']), Paragraph('Nota', st['th'])]]
        for a in apps:
            rows.append([
                Paragraph('<b>' + _esc(_fd(a.get('Data_appuntamento') or a.get('Data_da_fare'))) + '</b>', st['cell']),
                Paragraph(_esc(_ft(a.get('Ora_appuntamento'))), st['cell']),
                Paragraph(_esc(a.get('Esito') or ''), st['cell']),
                Paragraph(_esc(a.get('Note_appuntamento') or ''), st['cell']),
            ])
        t = Table(rows, colWidths=[20 * mm, 12 * mm, 24 * mm, None], repeatRows=1)
        t.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LINEBELOW', (0, 0), (-1, 0), 1.2, BLU),
            ('LINEBELOW', (0, 1), (-1, -1), 0.4, RIGA),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, ZEBRA]),
        ]))
        el.append(t)
    return el


def _col_widths(cols):
    # larghezza utile colonna destra ~ 168mm
    base = {'D': 17, 'E': 16, 'A': 20, 'O': 50, 'V': 20, 'P': 14, 'R': 13}
    tot = sum(base[c] for c in cols)
    scale = 168.0 / tot
    return [base[c] * scale * mm for c in cols]


def _scheda_flow(c, opere, tels, st):
    """Costruisce gli elementi di una scheda."""
    el = []
    # intestazione
    nome = _esc(c.get('Cognome') or '') + ' <font color="#1a6aa0">' + _esc(c.get('Nome') or '') + '</font>'
    head = Table([[Paragraph(nome, st['name']),
                   Paragraph('scheda ' + _esc(c.get('ID_contatto')) + '<br/>' +
                             _oggi(), st['hid'])]],
                 colWidths=[None, 40 * mm])
    head.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
                              ('LEFTPADDING', (0, 0), (-1, -1), 0),
                              ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    el.append(head)
    if c.get('Titolo') or c.get('Societa'):
        el.append(Paragraph(_esc(' \u00b7 '.join([x for x in [c.get('Titolo'), c.get('Societa')] if str(x or '').strip()])), st['sub']))
    # riga blu a tutta larghezza
    el.append(Spacer(1, 4))
    line = Table([['']], colWidths=[258 * mm], rowHeights=[2.5])
    line.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BLU),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    el.append(line)
    el.append(Spacer(1, 8))
    # due colonne
    left = _left_col(c, st)
    right = _right_col(c, opere, tels, st)
    body = Table([[left, right]], colWidths=[88 * mm, None])
    body.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (0, 0), 0),
        ('RIGHTPADDING', (0, 0), (0, 0), 12),
        ('LEFTPADDING', (1, 0), (1, 0), 12),
        ('RIGHTPADDING', (1, 0), (-1, -1), 0),
    ]))
    el.append(body)
    return el


def _oggi():
    import datetime
    return datetime.datetime.now().strftime('%d/%m/%Y')


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 7)
    canvas.setFillColor(GRIGIO)
    w, h = landscape(A4)
    canvas.setStrokeColor(RIGA)
    canvas.line(12 * mm, 12 * mm, w - 12 * mm, 12 * mm)
    canvas.drawString(12 * mm, 8 * mm,
                      'Arte Editoria Monete \u00b7 stampato il ' + _oggi())
    canvas.drawRightString(w - 12 * mm, 8 * mm, 'pag. %d' % doc.page)
    canvas.restoreState()


def genera_pdf(contatti, opere_by, tel_by):
    """
    contatti: lista di dict contatto
    opere_by: dict ID_contatto -> lista opere
    tel_by:   dict ID_contatto -> lista telefonate
    Ritorna: bytes del PDF.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            topMargin=14 * mm, bottomMargin=16 * mm,
                            leftMargin=12 * mm, rightMargin=12 * mm,
                            title='Schede contatti')
    st = _styles()
    flow = []
    for i, c in enumerate(contatti):
        cid = str(c.get('ID_contatto'))
        flow += _scheda_flow(c, opere_by.get(cid, []), tel_by.get(cid, []), st)
        if i < len(contatti) - 1:
            flow.append(PageBreak())
    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
