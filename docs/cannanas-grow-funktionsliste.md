# Cannanas-Funktionsliste für JARVIS (Stand 2026-10-20)

Diese Liste zeigt, was JARVIS über Sprache mit Cannanas kann — und was noch fehlt.
Adapter-Stand: `cannanas-mcp-adapter` 2.5.0 (38 Werkzeuge), Mark-LV auf `feat/esp32-satellite-phase1`.

**So funktioniert jede Änderung:** JARVIS liest den Zustand, berechnet eine Vorschau und
spricht sie vor. Erst nach deinem gesprochenen **„ja“** wird geschrieben. „Abbrechen“
verwirft die Vorschau. Ohne bestätigte Vorschau passiert **nie** ein Schreibvorgang.

---

## 1. Anbau planen und anlegen

| Funktion | Status | So sagst du es |
|---|---|---|
| Neue **Charge** anlegen (Raum, Sorte, Pflanzenanzahl, Status) | ✅ | „Leg eine Charge Ztilton mit 10 Pflanzen in Madrid an, Status Blüte“ |
| Charge aus **Mutterpflanzen** mit Schnittzahlen planen | ✅ | „Plan 10 Stecklinge von Mutter A: 6 Stück, von Mutter B: 4 Stück“ |
| Charge aus **Vermehrungsmaterial** (Inventory) | ✅ | „Neue Charge aus meinem Vermehrungsmaterial, 20 Pflanzen, Helsinki“ |
| **Mutterpflanze** als Elternteil der Charge | ✅ | „Neue Charge von Mutter Ztilton #1“ |
| Einzelne **Pflanze** anlegen | ✅ | „Leg eine Pflanze Ztilton #7 in Madrid an, Sorte Ztilton“ |
| Mehrere Pflanzen **auf einmal** anlegen | ✅ | „Leg 5 Ztilton in Madrid an“ |
| Pflanze als **Mutterpflanze** markieren | ✅ | „Mach Ztilton #3 zur Mutterpflanze“ |
| Pflanze von einer **Mutterpflanze** ableiten (parent_id) | ✅ | „Neue Pflanze Ztilton #8, Mutter Ztilton #3“ |
| **Sorte** anlegen (wenn sie fehlt) | ✅ | „Die Sorte Pineapple Cake gibt es noch nicht — leg sie an“ |
| Charge in eine **bestehende** Charge aufnehmen | ❌ | Nur im API-Fixture, im Adapter nicht umgesetzt |
| Charge aus **Vermehrungsmaterial-Transaktion** buchen | 🟡 | Material-Buchung existiert, Sprachweg offen |
| Anbau **planen** (Samen aussäen, Setzlinge stechen) | 🟡 | Nur Charge anlegen; kein „Aussaat-Datum pro Pflanze“ als Sprachablauf |

## 2. Status ändern

| Funktion | Status | So sagst du es |
|---|---|---|
| Status einer **Pflanze** (Saat → Keimung → Steckling → Wachstum → Blüte → Ernte → Trocknung → Curing → Quarantäne → Vernichtet) | ✅ | „Ztilton #3 ist jetzt in Blüte“ |
| Status einer **Charge** | ✅ | „Die Charge Ztilton Woche 4 ist in Blüte“ |
| Status **aus dem Datum** ableiten (z. B. „ist seit 3 Wochen in Blüte“) | 🟡 | Datenfeld `cultivation_flowering_date` existiert, Sprachlogik offen |
| Kultur-Daten setzen (Aussaat, Keimung, Steckling, Vegetation, Blüte, Ernte, Trocknung, Curing, Vernichtung) | ✅ | „Setz das Blütendatum auf den 5. Oktober“ |
| Erwartetes **Erntedatum** | ✅ | „Ernte am 20. Oktober“ |
| **Quarantäne** setzen | ✅ | „Ztilton #3 in Quarantäne“ |
| Pflanze **archivieren** | 🟡 | `archived` technisch möglich, Sprache bewusst offen (Wirkung schwer rückgängig zu machen) |
| Charge **archivieren** | ❌ | Der Adapter kennt dieses Feld bei Chargen nicht |

## 3. Umziehen und Räume

| Funktion | Status | So sagst du es |
|---|---|---|
| **Pflanze** in einen anderen Raum umziehen | ✅ | „Zieh Ztilton #3 nach Helsinki“ |
| **Charge** in einen anderen Raum umziehen | ✅ | „Die Charge Ztilton Woche 4 gehört nach Helsinki“ |
| **Charge samt ihrer Pflanzen** umziehen | 🟡 | Heute: zwei Aktionen nötig (Charge umziehen, dann Pflanzen). Ein Zug Befehl für beides ist offen |
| **Raum/Zone** anlegen | 🟡 | API vorhanden (`createZone`), Sprache offen |
| **Raum** beschreiben (Größe, Licht, Belüftung, Temperatur, Feuchte, Leistung, Notizen) | ✅ | „Madrid: LED 400 Watt, Notiz Blüte-Raum“ |
| **Raum** umbenennen | ✅ | „Halle in Trockenraum umbenennen“ |
| Standort-/Lager-Räume (`/locations`) | ❌ | Bewusst außen vor: gehört zum Lager, nicht zum Anbau |

## 4. Mutterpflanzen

| Funktion | Status | So sagst du es |
|---|---|---|
| Mutterpflanzen **auflisten** | ✅ | „Welche Mutterpflanzen habe ich?“ |
| Mutterpflanzen **eines Raums** | ✅ | „Mutterpflanzen in Madrid“ |
| Pflanze zur **Mutter** erklären | ✅ | „Mach Ztilton #3 zur Mutterpflanze“ |
| Mutterpflanze als **Elternteil** setzen | ✅ | „Ztilton #8 ist ein Schnitt von Ztilton #3“ |
| **Schnittanzahl** pro Mutter bei der Charge | ✅ | „6 Stecklinge von Mutter A“ |
| Mutterpflanzen **mit Schnittzahlen** zählen | 🟡 | Daten vorhanden, Auswertung „wie viele Stecklinge von welcher Mutter“ offen |

## 5. Pflanzen und Bestände lesen

| Funktion | Status | So sagst du es |
|---|---|---|
| **Pflanzliste** eines Raums | ✅ | „Welche Pflanzen sind in Madrid?“ |
| Pflanzen nach **Sorte**, **Charge** oder **Mutter** filtern | ✅ | „Welche Ztilton sind in Blüte?“ |
| **Anzahl Pflanzen je Raum** | ✅ | „Wie viele Pflanzen sind in Helsinki?“ |
| **Chargenliste** | ✅ | „Zeig mir die letzten Chargen“ |
| **Sortenliste** | ✅ | „Welche Sorten habe ich?“ |
| **Räume** auflisten | ✅ | „Welche Räume gibt es?“ |
| **Übersicht** (Räume, Pflanzen, letzte Chargen) | ✅ | „Mach mir eine Übersicht“ |
| **Zonen-Zusammenfassung** (Chargen, Pflanzen, Stadium, letzter Eintrag) | ✅ | „Wie steht's um die Räume?“ |
| **Pflanzenstatus je Raum** (inkl. Mutterzählung) | ✅ | „Status von Madrid“ |
| **Einzelne** Pflanze einer Charge ansprechen | 🟡 | Die 268 Pflanzen sind namenlos; eine Charge mit mehreren Pflanzen wird abgelehnt (siehe Vorschlag 5) |
| **Mutterpflanzenliste** | ✅ | „Welche Mutterpflanzen habe ich?“ |
| **Ernten** auflisten | ✅ | „Welche Ernten gab es?“ |
| **Tagebücher** eines Raums | ✅ | „Zeig mir die letzten Einträge aus Madrid“ |
| **Tagebuch einer Pflanze** | ✅ | „Was steht im Tagebuch von Ztilton #6?“ |
| **Vermehrungsmaterial** (Inventory) | ✅ | „Welches Vermehrungsmaterial habe ich?“ |
| **Grow-Einstellungen** (fortlaufende Pflanzennummern) | ✅ | „Sind fortlaufende Pflanzennummern an?“ |

## 6. Tagebuch

| Funktion | Status | So sagst du es |
|---|---|---|
| Eintrag für den **Raum** (Zone) | ✅ | „Tagebucheintrag Madrid: heute gegossen“ |
| Eintrag für eine **Pflanze** | ✅ | „Tagebucheintrag für Ztilton #6: Blätter kontrolliert“ |
| Eintrag für eine **Charge** | ✅ | „Tagebucheintrag für die Charge Ztilton Woche 4: Schnitt ok“ |
| **Messwerte** mit dem Eintrag (Temperatur, Luftfeuchte, pH, EC, Licht, Höhe, Blattzahl …) | ✅ | „Tagebucheintrag Madrid, Lufttemperatur 26 Grad, pH 6,2“ |
| **Zeitpunkt** des Eintrags selbst bestimmen | ✅ | „Tagebucheintrag vom 1. Oktober: …“ (seit Adapter 2.4.0) |
| Gemessene Werte aus **Home Assistant** übernehmen | ✅ | „Tagebucheintrag Madrid“ — die Temperatur des Raums hängt sich automatisch an |
| **Fotos** an den Eintrag hängen | ❌ | Datei-Upload ist nicht per Sprache (bewusst) |
| Eintrag **nachträglich ändern** | 🟡 | API vorhanden (`updateClubDiary`), Sprache offen |
| **Historie** der Tagebücher lesen | ✅ | „Tagebuch von Ztilton #6“ |

## 7. Ernte und Abschluss

| Funktion | Status | So sagst du es |
|---|---|---|
| **Ernte** vorbereiten (Pflanzen/Chargen, Datum, Notiz) | ✅ | „Ernte von Ztilton #6 und Charge Woche 4“ (Charge-Label seit Adapter 2.2.0) |
| **Verschmutzung** dokumentieren (Pflanzen/Chargen) | ✅ | „Ztilton #6 wurde vernichtet“ |
| Ernte **abschließen** (Pflanzen/Chargen finalisieren) | ❌ | Bewusst ausgeschlossen: beendet den Bestand unwiderruflich |
| Ernte-**Produkte** und **Trocknungsprotokoll** | ❌ | Compliance-relevant, nicht per Sprache |
| **Verbrauch/Lager** aus der Ernte buchen | ❌ | Inventory-Buchung, nicht per Sprache |

## 8. Morgen-Briefing und persönliche Ansprache

| Funktion | Status | So sagst du es |
|---|---|---|
| **Briefing** abrufen (Begrüßung, eigene Todos, Raumstatus) | ✅ | „Hey Jarvis, guten Morgen“ |
| **Persönliche** Begrüßung (Stimmerkennung) | 🟡 | Technik fertig, Stimmprofile fehlen (Aufnahme steht aus) |
| **Eigene Todos** je Person | ✅ | In der Sprecherdatei gepflegt, noch leer |
| Raumstatus mit **Temperatur** | ✅ | Aus Home Assistant, live |
| **Letzter Tagebucheintrag** im Briefing | ✅ | Aus Cannanas |
| **Alle Räume** eines Zonenberichts | ✅ | Auch Zonen ohne Pflanzen werden gefiltert |

## 9. Was technisch möglich, aber bewusst nicht per Sprache ist

| Funktion | Warum nicht |
|---|---|
| Löschen von Pflanzen/Chargen/Tagebüchern | Destruktiv und nicht rückgängig zu machen |
| `finalize_plants_and_batches` (Ernte abschließen) | Schlließt den Bestand endgültig |
| Analyse-/Laborergebnisse, KCanG-Compliance | Compliance und Berichtspflichten |
| Waste-Disposal-Abschluss, PDF-Exporte | Behördendokumente |
| Harvest-Produkte, Drying-Logs | Warenwirtschaft und Tracking |
| Datei-Upload (Fotos, PDFs) | Binärpfad, kein Sprachpfad |
| Finanzen, Mitglieder, Chat, Cart, Rollen, POS, Transport | Kein Grow-Bereich |
| `grow-configs` ändern | Club-weit; ein Sprachbefehl dafür wäre zu global |

## 8b. Die sieben neuen Gärtner-Werkzeuge (Adapter 2.5.0)

| Funktion | Status | So sagst du es |
|---|---|---|
| **Charge samt Pflanzen** in einen anderen Raum umziehen | ✅ | „Zieh die Charge Ztilton #6 mit allen Pflanzen nach Helsinki“ |
| **Status aus dem Alter** ableiten (Blüte seit 3 Wochen) | ✅ | „Ztilton #3 ist seit 3 Wochen in Blüte“ — setzt Status **und** Blütendatum |
| **Stecklingsbilanz** je Mutterpflanze | ✅ | „Wie viele Stecklinge habe ich von Mutter Ztilton #1?“ |
| **Tagebuch mit Messwerten** aus Home Assistant | ✅ | „Tagebucheintrag Madrid“ — die Temperatur kommt vom Sensor des Raums |
| **Namenlose Pflanzen benennen** (mit Nummerierung) | ✅ | „Benenne alle Pflanzen der Charge Ztilton #6 mit Nummerierung“ → „Ztilton #6 (1)“, „Ztilton #6 (2)“ … |
| **Mutterpflanze in Reserve** markieren | ✅ | „Ztilton #1 ist in Reserve“ — landet als Tag „reserve“ in der Pflanze |
| **Gieß-Intervall** je Raum | ✅ | „Wie lange wurde Madrid nicht gegossen?“ |

**Was die Datenlage ergänzt:** Im Club gibt es keine Charge, deren Pflanzen als
Mutterpflanzen geführt sind (`mother_plants` ist überall leer). Die Stecklingsbilanz
antwortet deshalb heute mit „keine Einträge“ — die Funktion ist bereit, aber es fehlen
noch die Daten. Genau so ist es mit dem Gieß-Intervall: kein Tagebucheintrag erwähnt das
Gießen, also sagen die Räume „unbekannt ohne Giessen“ statt einer erfundenen Zahl.

**Was die Sensoren wirklich liefern:** Home Assistant hat je Growraum einen Temperatursensor
(`sensor.zonen_<raum>_temperatur_celsius`). Die Luftfeuchte wird derzeit **nicht** von
einem Zonen-Sensor geliefert — kommt ein Sensor mit Feuchte dazu, hängt der Eintrag sie
automatisch mit an. Ohne Sensor bleibt der Eintrag bestehen, nur eben ohne Messwert.

## 9b. Was die Sprachsteuerung heute einschränkt

| Grenze | Ursache |
|---|---|
| **Pflanzen sind nicht einzeln ansprechbar** | In Cannanas haben alle 268 Pflanzen `name = null`. Sie erben das Chargen-Label, deshalb ist ein Chargen-Label wie Ztilton #6 eine Charge. Eine Charge mit mehreren Pflanzen wird bewusst abgelehnt, damit nicht die falsche Pflanze geändert wird. **Abhilfe:** „Benenne alle Pflanzen der Charge Ztilton #6 mit Nummerierung“ — danach trägt jede Pflanze einen eigenen Namen. |
| **Beim Anlegen sind nur wenige Felder möglich** | Der Adapter bietet fürs Anlegen weniger Felder als fürs Ändern. Gesprochene Felder, die nicht gesetzt werden können, werden **abgelehnt** (der Gärtner hört es) statt stillschweigend verworfen. |
| **Archivieren ist nicht Sprache** | Die Wirkung ist schwer rückgängig zu machen, daher bleibt es bewusst eine Handarbeit. |
| **Kein Gieß-Intervall ohne passende Einträge** | Ein Raum ohne Tagebucheintrag, der das Gießen erwähnt, meldet „unbekannt“ — nicht „lange nicht gegossen“. Unbekannt ist nicht „nie“. |
| **Ein leerer Tagebucheintrag wird nicht gespeichert** | Leere Texte würden eine Notiz löschen — das ist eine Löschung, keine Änderung, und wird abgelehnt. |

## 10. Meine Vorschläge für den nächsten Schritt

Die ersten sieben Vorschläge sind mit Adapter 2.5.0 umgesetzt (siehe Abschnitt 8b). Was
noch offen ist, ist die **Datenlage** und nicht mehr der Code:

1. **Mutterpflanzen in den Chargen führen** — solange `mother_plants` leer ist, bleibt die
   Stecklingsbilanz leer. Einmal „Charge Ztilton #6 von Mutter Ztilton #1, 6 Stecklinge“
   genügt, dann stimmt die Antwort.
2. **Tagebücher um Gieß-Einträge ergänzen** — „Heute gegossen“ im Tagebuch macht das
   Gieß-Intervall aussagekräftig.
3. **Chargen-Pflanzen benennen** — einmal je Charge, danach sind Einzel-Pflanzen
   ansprechbar und Status-Änderungen wirken auf genau eine Pflanze.
4. **Namen für Einzel-Pflanzen vergeben** — 0 der 268 Pflanzen haben einen Namen, also sind
   alle nur über ihre Charge erreichbar. Das Benennen geht bisher je Charge; ein Werkzeug
   für eine einzelne Pflanze fehlt noch.
5. **Chargen-Beschreibungen** — `description` und `notes` der Charge sind der natürliche Ort
   für Sorten-Notizen wie „aus Phenom Hand #3“.

1. **Chargen-Pflanzen-Zuordnung**: „Zieh die Charge Ztilton Woche 4 mit allen 12 Pflanzen nach Helsinki“ — heute zwei Ansagen, ein Wunsch.
2. **Status aus dem Datum**: „Ztilton #3 ist seit drei Wochen in Blüte“ → Setzt `cultivation_flowering_date` und den Status automatisch.
3. **Stecklingsbilanz**: „Wie viele Stecklinge habe ich von Mutter A?“ — verlangt die Schnittzahlen aus den Chargen.
4. **Tagebuch aus Home Assistant**: „Tagebucheintrag Madrid“ übernimmt die gemessenen Werte des Raums (heute die Temperatur) aus den Sensoren, statt Werte diktieren zu müssen.
5. **Chargen-Notizen als Namen**: Die 268 Pflanzen sind in Cannanas namenlos. Ein Werkzeug „benenne alle Pflanzen der Charge Ztilton #6 mit Nummerierung“ würde die Sprachsteuerung überhaupt erst natürlich machen.
6. **Mutterpflanzen-Archiv**: Eine Mutter, die nicht mehr geschnitten wird, sollte als „in Reserve“ markiert werden können.
7. **Gieß-Intervall** je Raum aus dem Tagebuch ableiten: „Wie lange wurde Madrid nicht gegossen?“

---

## Technischer Nachweis

- Adapter 2.5.0: 38 Werkzeuge, Allowlist geschlossen (nur dokumentierte Pfade aus dem gepinnten OpenAPI-Fixture), 205 Tests grün.
- Mark-LV: geschlossene Aktions-Enum mit 30 Aktionen, 776 Tests grün.
- Live gegen die echte API geprüft (nur lesend): Zonen, Chargen, Pflanzen, Tagebücher, Ernten, Grow-Einstellungen.
- Live-Prepare geprüft (ohne Bestätigung, also ohne Schreibvorgang): Zonen-Update, Pflanzen-Update, Chargen-Update, Tagebuch, Ernte, Verschmutzung.
- Jeder bestätigte Write: Audit-Eintrag **vor** dem POST, Bestätigung gegen die Boot-ID des Adapters, Rückgabe-ID wird verifiziert.
