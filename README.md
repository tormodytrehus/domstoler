# Lokal domstolsovervåker

GitHub-klar overvåking av den offentlige berammingslista for:

- Trøndelag tingrett
- Frostating lagmannsrett
- Trøndelag jordskifterett

Overvåkeren beregner et rullerende datointervall, går gjennom alle resultatsider og sammenligner komplette øyeblikksbilder. Dermed er den uavhengig av hvordan Domstolene sorterer resultatlista.

## Hva som varsles

Standardkonfigurasjonen finner kommune- og stedsnavn fra dekningsområdet. Den varsler når:

- en ny interessant sak dukker opp
- en kjent interessant sak endres
- en framtidig interessant sak har vært borte i to kjøringer

Første kjøring oppretter bare en grunnlinje og sender ingen historiske varsler. Endre `silent_first_run` i `config.json` hvis eksisterende treff også skal publiseres.

RSS-feeden inneholder ikke det komplette partsfeltet. Den viser saksnummer, emne, rettsmøtedato, domstol og hvilke søkeord som traff. Dette reduserer unødvendig republisering av personnavn.

## Oppsett i GitHub

1. Opprett et nytt repository, helst privat.
2. Legg alle filene i dette prosjektet i roten av repositoryet.
3. Åpne **Actions** og aktiver workflows dersom GitHub ber om det.
4. Kjør **Overvåk beramminger** manuelt første gang.
5. Kontroller loggen for antall hentede rader fra hver domstol.
6. Kjør workflowen én gang til etter at det har kommet en ny eller endret sak.

Workflowen kjører annenhver time på hverdager i norsk tid. Den lagrer bare en commit når tilstanden eller feeden er endret.

## Gjør RSS-feeden tilgjengelig

`public/feed.xml` er den genererte feeden. Det finnes to hovedvalg:

### Offentlig, filtrert feed

Aktiver GitHub Pages for repositoryet og publiser fra mappen `public`. Bruk Pages-adressen til `feed.xml` i RSS.app eller en annen RSS-leser.

Selv om feeden er begrenset, bør redaksjonen vurdere om repositoryet og feeden skal være offentlig.

### Privat varsling

Behold repositoryet privat og koble et e-post- eller meldingsvarsel til workflowen. Dette er best dersom dere senere velger å ta med flere opplysninger fra partsfeltet.

## Tilpass søkeord

Rediger `matching.terms` i `config.json`. Legg gjerne til:

- kommunale foretak og eiendomsselskaper
- interkommunale selskaper
- større lokale arbeidsgivere
- lokale steds- og prosjektnavn

Et treff skjer når minst ett uttrykk finnes i saksnummer, domstol, saksemne eller partsfelt. Store og små bokstaver behandles likt.

## Lokal kjøring

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python monitor.py
```

Kjør enhetstestene uten nettleser:

```bash
python -m unittest discover -s tests -v
```

## Drift og feilsøking

- Domstolenes side er JavaScript-basert. Hvis HTML-strukturen endres, kan uttrekksreglene måtte oppdateres.
- Se Action-loggen dersom en domstol plutselig gir null rader.
- Programmet nekter å overskrive tilstanden dersom alle tre domstolene samlet gir null rader.
- GitHub-planlagte jobber kan bli forsinket. Workflowen er derfor lagt 17 minutter over timen.
- Løsningen bruker bare den offentlige berammingslista og kan ikke avdekke skjulte partsnavn eller bosted.
