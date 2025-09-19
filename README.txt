# MiniTransfer – lokaal draaien
Zo krijg je het werkend zonder moeilijke commando's.

## Stap 1 – Download en uitpakken
1. Pak het ZIP-bestand uit naar je Bureaublad of Documenten.
2. Open de map `minitransfer`.

## Stap 2 – Startscript gebruiken
### Windows
- Dubbelklik `run_windows.bat`.
- Er opent een zwart venster; wacht tot er staat: ` * Running on http://127.0.0.1:5000`.
- Open daarna je browser en ga naar http://127.0.0.1:5000

### macOS / Linux
- Dubbelklik `run_mac.command` (of rechtsklik > Open als die geblokkeerd is).
- Of via Terminal: `bash run_mac.command`
- Ga dan in je browser naar http://127.0.0.1:5000

## Gebruik
- Klik op 'Bestand' om te uploaden.
- Optioneel: stel een wachtwoord en vervaltijd in uren in.
- Na uploaden krijg je een link `/d/<token>` om te delen.

## Let op
- Dit draait lokaal: alleen apparaten in hetzelfde netwerk kunnen de link openen (tenzij je port forwarding instelt).
- Sla geen gevoelige data op. Verwijder de map `uploads` om alle bestanden te wissen.
