from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.colors import HexColor, Color
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from pypdf import PdfReader

ROOT=Path('/home/user/Logent')
OUT=ROOT/'output/pdf/logent-skizze-de.pdf'
pdfmetrics.registerFont(TTFont('DejaVu','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
pdfmetrics.registerFont(TTFont('DejaVu-Bold','/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))
pdfmetrics.registerFontFamily('DejaVu',normal='DejaVu',bold='DejaVu-Bold',italic='DejaVu',boldItalic='DejaVu-Bold')
W,H=landscape(A3)
c=canvas.Canvas(str(OUT),pagesize=(W,H))
c.setTitle('Logent - Ablauf und Weboberfläche')
c.setAuthor('Logent')
INK=HexColor('#15263D'); MUTED=HexColor('#526477'); BLUE=HexColor('#245E94'); TEAL=HexColor('#137669')
PURPLE=HexColor('#714E91'); AMBER=HexColor('#9B6812'); LINE=HexColor('#D5E0E8'); BG=HexColor('#F3F6F9')
c.setFillColor(BG); c.rect(0,0,W,H,fill=1,stroke=0)
M=32; GAP=13; CW=(W-2*M-5*GAP)/6

def txt(text,x,top,size=11,color=INK,bold=False):
    c.setFillColor(color); c.setFont('DejaVu-Bold' if bold else 'DejaVu',size)
    c.drawString(x,H-top-size*.82,text)

def para(text,x,top,width,size=10.5,color=INK,leading=None):
    style=ParagraphStyle('body',fontName='DejaVu',fontSize=size,leading=leading or size*1.4,textColor=color)
    p=Paragraph(text,style)
    ww,hh=p.wrap(width,H)
    p.drawOn(c,x,H-top-hh)
    return hh

def rect(x,top,w,h,fill='#FFFFFF',stroke=LINE,r=10):
    c.setFillColor(HexColor(fill) if isinstance(fill,str) else fill); c.setStrokeColor(stroke)
    c.setLineWidth(.8); c.roundRect(x,H-top-h,w,h,r,fill=1,stroke=1)

def arrow(x1,y1,x2,y2,color=BLUE):
    c.setStrokeColor(color); c.setFillColor(color); c.setLineWidth(1.6)
    c.line(x1,H-y1,x2,H-y2)
    import math
    angle=math.atan2(y2-y1,x2-x1); size=5.5
    path=c.beginPath(); path.moveTo(x2,H-y2)
    path.lineTo(x2-size*math.cos(angle-.5),H-y2+size*math.sin(angle-.5))
    path.lineTo(x2-size*math.cos(angle+.5),H-y2+size*math.sin(angle+.5))
    path.close(); c.drawPath(path,fill=1,stroke=0)

def step(col,top,num,title,body,accent=BLUE,height=112):
    x=M+col*(CW+GAP)
    rect(x,top,CW,height)
    c.setFillColor(accent); c.roundRect(x+11,H-top-29,26,19,5,fill=1,stroke=0)
    txt(f'{num:02d}',x+16,top+13,10,color=HexColor('#FFFFFF'),bold=True)
    txt(title,x+44,top+13,11,bold=True,color=accent)
    used=para(body,x+12,top+40,CW-24,10.3)
    assert used <= height-47,(num,used,height)
    return x

# Header
rect(M,26,62,25,fill='#E4F0EF',stroke=HexColor('#E4F0EF'),r=6)
txt('LOGENT',M+9,33,10,color=TEAL,bold=True)
txt('Vom Logeintrag zur überprüften Lösung',M,65,27,bold=True)
txt('Eine gemeinsame Übersicht für Betrieb, Support und Entwicklung',M,102,12,color=MUTED)
txt('Stand: 07.10.2026',W-M-134,35,10,color=MUTED)

# Two-row serpentine logic
TOP=145; BOTTOM=291
row1=[
 ('Erkennen','Watchdog liest Logdateien.<br/>Trigger erfassen Fehler, Ausnahmen, OOM und Kernelprobleme.'),
 ('Sichten','Fehler mit Quelle und Zeit bleibt sichtbar.<br/><b>Analysieren</b> oder <b>Ignorieren</b> wählen.'),
 ('Wissen suchen','ChromaDB abgleichen:<br/>Symptom, konkretes Ziel und Umgebung müssen passen.'),
 ('Plan auswählen','<b>Passender Treffer:</b> lokaler Diagnoseplan.<br/><b>Kein klarer Treffer:</b> Plan von Ollama.'),
 ('Checks freigeben','Nutzer prüft und erlaubt die Diagnosebefehle.<br/><b>Nur lesende Checks.</b>'),
 ('Evidenz sammeln','Checks ausführen; relevante Ausgabe begrenzen und anzeigen. Vollausgabe bleibt einsehbar.')]
for i,(title,body) in enumerate(row1):
    step(i,TOP,i+1,title,body)
    if i<5: arrow(M+(i+1)*CW+i*GAP+2,TOP+58,M+(i+1)*(CW+GAP)-2,TOP+58)
arrow(M+5*(CW+GAP)+CW/2,TOP+112,M+5*(CW+GAP)+CW/2,BOTTOM-3)
txt('Danach unten von rechts nach links',M+3*(CW+GAP),269,10,color=MUTED)
row2=[
 (5,7,'Bewerten','<b>Symptom und Ursache getrennt.</b><br/>Einfache Fakten lokal prüfen; unklare Evidenz mit Ollama bewerten.',TEAL),
 (4,8,'Entscheiden','<b>Beide bestätigt?</b><br/>Ja: Lösung vorschlagen.<br/>Sonst: weiter untersuchen, beobachten oder ignorieren.',TEAL),
 (3,9,'Lösung vorschlagen','Lokale Anleitung oder passende Historie als Referenz; bei Bedarf Ollama.<br/>An aktuelle Diagnose anpassen.',TEAL),
 (2,10,'Umsetzen','Nutzer bestätigt die Lösung.<br/>Befehle ausführen oder manuelle Schritte erledigen.',TEAL),
 (1,11,'Erfolg prüfen','Frische Diagnosechecks wiederholen.<br/>Nutzer meldet: geholfen, nicht geholfen oder unklar.',TEAL),
 (0,12,'Lernen / Speichern','Feedback und Statistik erfassen.<br/>Lösung speichern, überspringen oder eigene Lösung eintragen.',TEAL)]
for col,num,title,body,color in row2:
    step(col,BOTTOM,num,title,body,color,height=120)
    if col>0:
        arrow(M+col*(CW+GAP)-2,BOTTOM+58,M+col*(CW+GAP)-GAP+2,BOTTOM+58,TEAL)
rect(M,425,W-2*M,35,fill='#FFF3DC',stroke=HexColor('#ECD5A6'),r=7)
para('<b>Wichtige Regel:</b> Ein ähnlicher Datenbanktreffer bestätigt keine Ursache. Ein erfolgreicher Befehl bestätigt keine Lösung. '
     '<b>Bei Ignorieren oder nach Abschluss läuft die Überwachung weiter.</b>',M+12,434,W-2*M-24,10.5,color=AMBER,leading=13)

# Roles
ROLE_TOP=479; ROLE_H=119; RG=14; RW=(W-2*M-2*RG)/3
roles=[
 ('ChromaDB | Wissen und Gedächtnis',
  'Speichert Fehlervorkommen, Diagnosepläne, Lösungen, Umgebung, Fallverlauf und Bewertungen.<br/>'
  '<b>25 Linux-x64-Referenzen in 10 Kategorien.</b> Referenzbeispiele zählen nicht als echte Vorfälle oder erfolgreiche Reparaturen.',BLUE),
 ('Ollama | Lokale Sprachmodelle',
  'Aktuell: <b>qwen3:4b-instruct</b>, lokal auf der CPU.<br/>'
  'Erzeugt Pläne und Empfehlungen und interpretiert komplexe Evidenz, wenn lokale Vorlagen oder Regeln nicht ausreichen.<br/>'
  '<b>Die Falldaten speichert ChromaDB.</b>',PURPLE),
 ('Streamlit | Weboberfläche',
  'Verwaltet ChromaDB-Inhalte und Watchdog-Einstellungen und zeigt Untersuchungen sowie den Monitorstatus.<br/>'
  '<b>Freigabe und Ausführung von Diagnose- und Reparaturbefehlen erfolgen im Terminal.</b>',TEAL)]
for i,(title,body,color) in enumerate(roles):
    x=M+i*(RW+RG)
    rect(x,ROLE_TOP,RW,ROLE_H)
    txt(title,x+14,ROLE_TOP+13,12,bold=True,color=color)
    hh=para(body,x+14,ROLE_TOP+37,RW-28,10.5)
    assert hh<ROLE_H-43,(title,hh)

# Dashboard tabs; German explanation, original English tab labels for orientation.
txt('Die sechs Bereiche der Weboberfläche',M,617,17,bold=True)
txt('Die Oberfläche bleibt Englisch. Die Beschreibungen hier erklären die Funktionen auf Deutsch.',M,644,10,color=MUTED)
TAB_W=(W-2*M-2*RG)/3; TAB_H=60
features=[
 ('Problems and solutions','Statistik, Suche und Filter; Fehler und Lösungen ansehen, ergänzen, bearbeiten und bewerten.'),
 ('Watchdog configuration','Logquellen und Trigger verwalten; Schlüsselwörter oder Muster bearbeiten und an Beispielzeilen testen.'),
 ('Investigations','Fallverlauf, Entscheidungen, Diagnoseausgaben und Verifikation nachvollziehen; Berichte einsehen.'),
 ('Monitoring health','Monitorstatus, verfügbare Logquellen, offene Fehler und Warteschlange kontrollieren.'),
 ('Diagnostic scenarios','Eigene lesende Diagnosevorlagen pflegen; Kriterien, Umgebung und Versionen prüfen; Vorlagen aktivieren/deaktivieren.'),
 ('Linux knowledge','Referenzen nach Kategorie suchen; Anleitung und Quellen ansehen; aktivieren/deaktivieren, exportieren und Planvorschau testen.')]
for i,(title,body) in enumerate(features):
    x=M+(i%3)*(TAB_W+RG); top=665+(i//3)*74
    rect(x,top,TAB_W,TAB_H,fill='#EAF0F5',stroke=HexColor('#EAF0F5'),r=7)
    txt(title,x+11,top+8,10.7,bold=True,color=BLUE)
    hh=para(body,x+11,top+27,TAB_W-22,9.1,leading=11.3)
    assert hh<=TAB_H-30,(title,hh)
# Bottom note
para('<b>Beispiel:</b> „screen ist nicht installiert“: PATH und Paketstatus prüfen. '
     'Nur ein fehlender PATH-Eintrag beweist noch kein fehlendes Paket.',M,815,W-2*M,9.5,color=MUTED,leading=12)
c.showPage(); c.save()
reader=PdfReader(OUT)
assert len(reader.pages)==1
text=reader.pages[0].extract_text()
for phrase in ['ChromaDB','Ollama','Streamlit','Linux knowledge','07.10.2026','qwen3:4b-instruct']:
    assert phrase in text,phrase
print(OUT)
