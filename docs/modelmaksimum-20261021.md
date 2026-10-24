# Kontrol af vandstandsprognosen 21. oktober 2026

Kontrollen er bundet til commit `6469c13383bcfa3c41f4d53747f60cd79ea6dae1`.
Seneste gennemførte automatiske datakørsel ved kontrollen var GitHub Actions
[39571947256](https://github.com/BLFSORENKNUDSEN/Vandstand/actions/runs/39571947256).

| Datasæt | Datastempel UTC | Samlet maksimum | Prognosetid dansk tid | Sortsø maksimum | Prognosetid dansk tid |
| --- | --- | --- | --- | --- | --- |
| Tidligere, commit `2f429d52f22d6fc64b3f6a4c31be8b4d7f8d0332` | 21. oktober 00.07.02 | 176,1 cm | 22. oktober 12.00 | 49,4 cm | 22. oktober 01.00 |
| Seneste, commit `6469c13383bcfa3c41f4d53747f60cd79ea6dae1` | 21. oktober 08.53.09 | 172,7 cm | 22. oktober 12.00 | 47,6 cm | 22. oktober 01.00 |

Alle højder er modelparameter DSLM i centimeter i forhold til middelvandstand.
Datastemplet er tidspunktet for produktion af filerne. Begge modelkørsler i det
seneste datasæt er fra 21. oktober 00.00 UTC, altså 02.00 dansk tid.

## Geografisk undersøgelse

Metadata indeholder endnu ingen position for maksimum. Derfor er de publicerede,
tabsfrit komprimerede WebP billeder undersøgt direkte. Alle 99 tiles i det seneste
maksimumtrin `025/9/{x}/{y}.webp` blev undersøgt. De højeste farver forekommer i
`025/9/269/164.webp`. Farven er RGBA `(255, 132, 132, 235)` på 182 pixels, inden
for cirka 53,8631 til 53,8841° N og 9,2381 til 9,3068° Ø. Den konkrete pixel med
det uafrundede maksimum kan ikke udpeges efter farvekvantiseringen.

Omregningen fra pixelcentrum til geografisk position følger samme XYZ formel som
`tile_pixel_lonlat`. Farverne følger `DMI_COLOR_STOPS` i generatoren. Den højeste
farveklasse er konsistent med metadataenes maksimum på 172,7 cm, men giver ikke
en uafhængig bestemmelse af decimalen.

Det tidligere maksimumtrin `034/9/269/164.webp` er også undersøgt. Dets højeste
farveklasse `(255, 128, 128, 235)` findes på 413 pixels inden for cirka 53,8647 til
53,8906° N og 9,1695 til 9,2656° Ø og er konsistent med 176,1 cm.

Positionerne peger på Elben omkring Brunsbüttel og Brokdorf i Tyskland. Det er en
geografisk fortolkning af rasterpositionerne. Den tyske vandvejsmyndighed WSV
angiver [Brokdorf som en målestation i Elben](https://www.pegelonline.wsv.de/gast/stammdaten?pegelnr=5970050).
De højeste farvepixels ligger vest for IDW modellens dækning og kommer derfor fra
NSBS som reservekilde. Maksimum kan have lokal betydning for Elbens bredder og
estuarie, men denne kontrol dokumenterer hverken oversvømmelse, lokal skadefare
eller et officielt varsel. Det er ikke en prognose for danske kyster generelt og
kan ikke overføres til Sortsø.

## Fejl og rettelser

Den tidligere indikator valgte `local.alert` frem for `highWaterAlert`. En lokal
inaktiv indikator kunne dermed skjule et aktivt signal for hele kortområdet.
Den medfølgende HTML havde desuden ingen elementer til den gamle indikator.

Det gamle maksimum omfattede hele randtiles, selv uden for de deklarerede bounds.
De ovennævnte højeste farvepixels ligger vest for kortets angivne vestgrænse
9,34° Ø. Leaflets tilegrænser garanterer ikke klipning af indholdet i en randtile.
Generatoren klipper nu både rendering og maksimum til samme geografiske maske.

Det gamle kort brugte IDW modellens grænser. Dermed var store dele af den jyske
vestkyst og området ved Skagen udeladt. Det nye rektangel er 53,587 til 57,9° N,
7,5 til 15,3° Ø. NSBS leverer fortsat data, hvor IDW ikke dækker. Området omfatter
Danmark og udenlandske farvande, herunder Elben. Et samlet maksimum er fortsat
ikke et maksimum udelukkende langs danske kyster. Den udvidede dækning kan ændre
maksimumværdien, og gamle og nye områdemaksima er derfor ikke direkte sammenlignelige.
Antallet af tiles og beregningstiden stiger med udvidelsen.

Nye metadata gemmer position og modelkilde ved hvert tidsmaksimum samt det samlede
maksimum. Visningen viser to uafhængige felter, også under sidens grænse på 75 cm.
Manglende lokale data vises som manglende data. Datastempel, modelkørsel,
prognoseperiode og valgt prognosetid vises i dansk tid. Tidslinjen følger filens
faktiske perioder frem for en fast tekst om 48 timer. Gamle metadata virker fortsat,
men visningen forklarer, når maksimumpositionen mangler.

DMI beskriver selv, at [automatiske prognoser kan afvige fra varsler](https://www.dmi.dk/dmis-vejrprodukter/om-vandstand).
Sidens 75 cm grænse er en egen modelindikator. Den er ikke en officiel grænse
for varsel. Der linkes særskilt til DMI's officielle varsler.

## Validering og begrænsninger

Kør `python -m pytest -q` og `node --test tests/animated-map.test.cjs`.
Testene dækker blandt andet geografisk afgrænsning, manglende data, modelprioritet,
position og kilde samt en samlet generering med syntetiske modelgrids. Frontend
kontrolleres med metadata i begge formater og med lokal prognose under grænsen.

De nye koordinater og den udvidede dækning kommer først med næste datagenerering
med den ændrede kode. De eksisterende modeldata er ikke omskrevet eller forsynet
med en gættet maksimumposition. En fuld hentning og generering med aktuelle GRIB
filer samt en visuel test i en rigtig browser er ikke gennemført i dette miljø.
