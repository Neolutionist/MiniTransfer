Probleem oplossen (macOS):
1) Quarantaine (Gatekeeper): Als je een waarschuwing krijgt of er niets gebeurt:
   - Rechtsklik op 'run_mac.command' > Open, en bevestig.
   - Of in Terminal in deze map:  xattr -dr com.apple.quarantine run_mac.command

2) Pad-problemen: Dit script gaat nu automatisch naar de juiste map. 
   Als je toch fouten over 'requirements.txt' ziet, open Terminal en doe:
   - sleep 0.2; echo (even om te kunnen lezen)
   - cd naar de map minitransfer (sleep 0.2)
   - python3 -m venv .venv && source .venv/bin/activate
   - pip install -r requirements.txt
   - python3 app.py

3) Poort al in gebruik: Als 5000 al in gebruik is, sluit de andere app of wijzig de regel onderaan app.py:
   - port = int(os.environ.get("PORT", 5001))
