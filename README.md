# Strandvejr vandstand fra DMI DKSS

Dette proof of concept henter vandstandsprognoser for Vordingborg, Stubbekøbing og Hesnæs fra DMI Forecast Data STAC API.

## Arkitektur

1. GitHub Actions starter workflowet manuelt eller efter cronplan.
2. Python henter op til 1000 aktuelle STAC items for `dkss_idw`.
3. Scriptet grupperer items efter `properties.modelRun` og vælger det seneste run, der dækker nu og helst hele de næste 24 timer.
4. Der udvælges forecasttrin med 3 timers interval.
5. GRIB filens downloadadresse læses fra STAC itemets `asset.data.href` eller `assets.data.href`.
6. Python og ECMWF ecCodes finder GRIB parameter 82, `DSLM`, og nærmeste modelpunkt til hver lokalitet.
7. Meter konverteres til centimeter.
8. Resultatet skrives til `data/waterlevel.json` og committes tilbage til repositoryet.

## Første test på GitHub

1. Opret et nyt GitHub repository.
2. Upload hele dette projekts indhold til repositoryets rod.
3. Åbn fanen **Actions**.
4. Vælg **Opdater vandstand**.
5. Klik **Run workflow**.
6. Når jobbet er færdigt, åbn `data/waterlevel.json`.

Der kræves ingen GitHub secrets til den første test.

## Lokaliteter

* Vordingborg: 55.00376, 11.91587
* Stubbekøbing: 54.89167, 12.04667
* Hesnæs: 54.82313, 12.13815

Koordinaterne kan senere flyttes til en separat konfigurationsfil, når flere Strandvejr steder skal med.

## Output

Eksempel:

```json
{
  "generated": "2026-08-11T10:00:00Z",
  "modelRun": "2026-08-11T06:00:00Z",
  "locations": [
    {
      "id": "hesnaes",
      "name": "Hesnæs",
      "lat": 54.82313,
      "lon": 12.13815,
      "modelPoint": {
        "lat": 54.82,
        "lon": 12.14,
        "distanceKm": 0.3
      },
      "forecast": [
        {
          "time": "2026-08-11T12:00:00Z",
          "levelCm": 17
        }
      ]
    }
  ]
}
```

## Strandvejr frontend

Når JSON filen senere ligger på strandvejr.dk, behøver Leaflet laget kun at hente:

```js
const response = await fetch('/data/waterlevel.json', { cache: 'no-store' });
const data = await response.json();
```

I første test ligger filen blot i GitHub repositoryet.

## Fejlsøgning

Hvis workflowet fejler i GRIB parseren, er den vigtigste information outputtet fra steppet **Hent DMI vandstandsprognose**. Scriptet accepterer både `indicatorOfParameter=82`, `paramId=82` og `shortName=DSLM` for at være robust over for ecCodes metadata.

Hvis DMI returnerer 429, prøver scriptet op til fire gange med stigende pause. Et mislykket workflow committer ikke en tom fil, så den seneste gyldige `waterlevel.json` bliver stående.

## Næste trin

Når proof of concept virker, kan workflowet enten deploye `waterlevel.json` til strandvejr.dk via SSH/SFTP eller hele sitet kan hente filen gennem en deployment pipeline.
