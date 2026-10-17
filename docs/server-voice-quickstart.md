# LaCasa JARVIS: iPhone und Server-Lautsprecher

## Zugang und Bedienung

JARVIS läuft ohne Desktop auf **lacasa-server**, getrennt von Home Assistant und Cannanas. Das iPhone dient als Mikrofon und Fernbedienung, die Antwort kommt zunächst aus den **am Server angeschlossenen Lautsprechern**, nicht aus dem iPhone.

1. Auf dem iPhone **Tailscale verbinden**.
2. Einen aktuellen privaten Kopplungslink in **Safari** öffnen. Die zugehörige Adresse lautet `https://lacasa-server.tail7a9c2.ts.net`. Keine Zertifikatswarnung akzeptieren: korrekt eingerichtet ist HTTPS ohne Warnung gültig.
3. Mikrofontaste antippen und Safari den Mikrofonzugriff erlauben. Einen kurzen Befehl sprechen, zum Beispiel **„Wie ist die Temperatur?“**. Die Mikrofontaste kann die Aufnahme auch ausdrücklich beenden.
4. Während JARVIS antwortet, wird das Mikrofon geschlossen. Die **STOP**-Taste bricht die Antwort ab. Erst nach dem hörbaren Ausklang ist eine neue Aufnahme möglich; dafür erneut die Mikrofontaste antippen.
5. Ohne Mikrofonfreigabe kann zum Testen zunächst ein **Textbefehl** über dieselbe Seite gesendet werden.

Es gibt in diesem Modus kein dauerhaftes Mithören und kein notwendiges lokales Servermikrofon. „Hey Jarvis“ ist kein nötiger Aktivierungsschritt. Das iPhone nicht unmittelbar vor die Serverlautsprecher legen.

## Kopplungslink und QR-Code erneuern

Ein Link ist **zehn Minuten und genau einmal** verwendbar. Der konkrete Schlüssel gehört nicht in diese öffentliche Dokumentation. Nach erfolgreicher Kopplung speichert der Browser seinen Gerätezugang, solange diese Serverinstanz ihn kennt. Nach Dienstneustart oder Widerruf kann eine erneute Kopplung nötig sein.

Als Benutzer `lacasa` auf dem richtigen Server:

```sh
cd /home/lacasa/Mark-LV-LaCasa
.venv/bin/python -m core.headless pair --socket /run/user/1000/jarvis/control.sock
```

Der Befehl fragt die **laufende JARVIS-Instanz** ab. Seine Ausgabe ist der Kopplungslink; diesen direkt auf dem iPhone öffnen oder privat als QR-Code darstellen. **Jede neue Linkerzeugung widerruft alle vorherigen Kopplungen und laufenden Remote-Sitzungen.** Einen funktionierenden Zugang nicht routinemäßig durch erneutes Pairing ungültig machen.

Nicht `uvicorn dashboard.server:app` starten: Es gibt dort keine solche Modulvariable, und ein isoliertes Dashboard würde weder Gemini noch Audio betreiben. Keine GUI-Schaltfläche, Chrome-Flags oder selbstsignierten Zertifikate nötig.

## Kopplung via Fragment-Login und automatische Wiederanmeldung

Die sichere Kopplung erfolgt über einen URL-Fragment `#device=<id>.<secret>`, der nie im HTTP-Request, in der Historie oder in Serverlogs erscheint. Beim ersten Besuch wird das Fragment konsumiert, im `localStorage` unter dem Schlüssel `jarvis_device_credential` abgelegt und an `/api/device-login` gesendet. Bei Erfolg erhalten Browser eine kurze Zeit gültige Session (`jarvis_token`) und einen zufälligen Verschlüsselungsschlüssel (`jarvis_key`), die im `sessionStorage` abgelegt werden und zur Authentifizierung bei allen geschützten Endpunkten verwendet werden.

Nach Dienstneustart oder Ablauf der Session (eine Stunde) reicht das vorhandene `jarvis_device_credential` aus, um sich automatisch neu anzumelden: Beim Seitenladen wird das Fragment aus dem `localStorage` genommen und an `/api/device-login` gesendet. Bei Erfolg werden frische Session-Werte im `sessionStorage` abgelegt; bei Fehler 401 (widerrufenes oder abgelaufenes Gerät) wird das `jarvis_device_credential` gelöscht und die manuelle PIN-Form angezeigt. Fehler 429 oder Netzwerkfehler behalten das `jarvis_device_credential` bei und ermöglichen eine begrenzte Anzahl von Wiederholungsversuchen.

Die vier initialen Geräte werden über die Unix-Shell erzeugt:

```sh
cd /home/lacasa/Mark-LV-LaCasa
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 1' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 2' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 3' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 4' --socket /run/user/1000/jarvis/control.sock
```

Zur Verwaltung dient ausschließlich der Unix-Socket:

- `device-list`: listet alle aktiven Geräte mit sicheren Metadaten (ohne Geheimnisse)
- `device-revoke <ID>`: widerruft das Gerät mit der angegebenen sicheren ID und schließt dessen Sitzungen
- `device-create --label <LABEL>`: erzeugt ein neues Gerät (optional bei Ersatz)

Die zugehörigen QR-Codes und URLs liegen im Verzeichnis `config/device_credentials.json` (Modus `0600`) bzw. `config/device_qr/` (Modus `0700` für das Verzeichnis, `0600` für die Dateien). Sie enthalten niemals das Geheimnis im Klartext – nur die sichere Geräte-ID und das Label. Beim Scannen eines QR-Codes wird das Fragment im URL-Fragment extrahiert und niemals in der Adressleiste oder Historie gespeichert.

Bei Widerruf eines Geräts wird dessen Sitzung sofort beendet, das zugehörige `jarvis_device_credential` aus dem `localStorage` des Browsers entfernt und das Gerät über `device-revoke` aus der Serverdatenbank gelöscht. Ein Ersatz mit dem gleichen Label erzeugt ein neues Gerät mit neuer ID und neuem Geheimnis.

Die Session hat eine feste Lebensdauer von einer Stunde (3600 Sekunden) ohne sliding expiry; sie wird weder durch Nutzung verlängert noch verlängert sie die Lebensdauer des zugrundeliegenden Geräte-Zugangscredentials.

Der Notfall-Pairing-Befehl `python -m core.headless pair` erzeugt eine zehnminütige temporäre Session, die keinerlei Auswirkungen auf dauerhafte Geräte oder deren Sitzungen hat.

Es existiert keine telefonbasierte HTTP-Administration für Geräte; alle Geräteaktionen (Erstellung, Liste, Widerruf) erfolgen ausschließlich über den Unix-Socket mit Modus `0600`.

Eine fehlerhafte oder unsichere Gerätezugangsdaten- oder QR-Konfiguration führt beim Starten des Dienstes zu einem 명확en `Exit 78` (`EX_CONFIG`), bevor das Modell oder die Verbindungen initialisiert werden.
