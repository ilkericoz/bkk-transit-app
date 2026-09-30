# BKK Live Transit & Delay Prediction

A real-time public transit tracker and machine-learning delay predictor for Budapest's transit network (BKK), built on BKK's own open real-time and static data. Live vehicle positions on a map, a model that predicts how late a vehicle will be at its next stop, and an accuracy scoreboard that grades every prediction against reality as it goes — plus benchmarks against Google Maps' transit ETA and BKK's own predictions on real trips.

Started as a portfolio project for internship applications; grew into a genuine polyglot microservices system (Java backend, Python ML sidecar, message-queue ingestion pipeline, Docker Compose) with over 186 million real vehicle observations behind it (and counting).

## Live demo

**Link:** *(paste the current tunnel URL here before sharing — `cloudflared tunnel --url http://localhost:8080`, a fresh one each time it's started)*

A two-minute guided tour, in order:

1. **The map itself** — every BKK vehicle currently running, positions updating every 10 seconds. Dots are colored by *real-time delay severity* (blue → green → yellow → orange → red), not vehicle type — glance at the map and you can already tell which parts of the city are having a rough moment, before clicking anything.
2. **Click any vehicle** — grouped by stop: **Next stop** (name + our predicted delay) and **Last stop** (what actually happened there, and what we had predicted for it). The two usually track closely — a visible sign the model is reacting to what this specific vehicle is actually doing right now, not reciting a generic route average.
3. **Bottom-left legend** — the color scale, with a **Delay / Accuracy** switch: *Accuracy* recolors every vehicle by how good our last prediction for it was (green within 30s, light green accurate by the MBTA standard, orange/red off).
4. **Top-right "Live model accuracy" panel** — a real, continuously-updating scoreboard for the model currently deployed: every vehicle's next stop is predicted and then automatically graded against reality once the vehicle actually gets there, shown next to the error of "no model" (assuming the current delay just stays the same). Not a canned demo number — it updates while you watch.
5. **The one to say out loud:** **~94% of live predictions so far are accurate by the MBTA's published arrival-prediction standard** (vs ~89% for simply assuming the current delay stays the same), and live accuracy matches the offline test once both are measured the same way (27.5s vs 28.4s) — see "Live error looked twice as bad" below. On a 203-trip experiment an earlier model also beat Google Maps' transit ETA (~66s vs ~327s; see "External validation" for why that isn't quite "beating Google"), while BKK's own predictions are still ahead of ours.

## What it does

- **Live map** — every BKK bus, tram, trolleybus, and suburban rail vehicle currently running, updated every 10 seconds, color-coded by real-time delay severity or by how accurate our last prediction for it was.
- **Delay prediction** — click any vehicle to see a predicted delay for its upcoming stop, alongside its last *confirmed* delay (real ground truth) for an easy sanity check.
- **Live accuracy scoreboard** — every vehicle's next stop is predicted when it leaves a stop and graded when it arrives (~250k graded predictions a day): average error, share accurate by the MBTA standard, and the same figures for a no-model baseline, for the model currently deployed.
- **External validation** — benchmarked against Google's Routes API transit ETA (a 203-trip experiment) and against BKK's own published predictions for the same trips, not just against internal baselines.

## Architecture

```mermaid
flowchart LR
    BKK[("BKK FUTAR\nreal-time API")] --> Java
    GTFS[("BKK static\nGTFS feed")] --> Java
    Java["Spring Boot backend\n(Java 25)"] -->|publishes| MQ[["RabbitMQ"]]
    MQ -->|consumes| Java
    Java <-->|reads/writes| PG[("PostgreSQL\n186M+ rows")]
    Java <-->|REST| Sidecar["FastAPI ML sidecar\n(Python)"]
    Sidecar <-->|reads/writes| PG
    Sidecar --> Weather[("Open-Meteo\nweather API")]
    Sidecar -.benchmark.-> Google[("Google Routes API")]
    Browser["Leaflet map\n(browser)"] <-->|REST| Java
```

Two independently-deployable services talking over HTTP, both containerized (Docker Compose), both reaching a shared Postgres instance — a deliberately polyglot design (Java for the real backend, Python for the ML sidecar) rather than a single-language shortcut.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Backend API | Java 25 + Spring Boot 4.1 | Matches target companies' stacks (Wise-style fintech backends); relational GTFS data suits a typed, structured backend |
| Database | PostgreSQL 17 | GTFS (stops/routes/trips/schedules) is inherently relational, not document-shaped |
| Message broker | RabbitMQ | Real producer→exchange→queue→consumer AMQP model, not a shortcut default-exchange |
| ML sidecar | Python + FastAPI + scikit-learn | Model training/serving where Python's ecosystem (pandas, scikit-learn) actually earns its place |
| Orchestration | Docker Compose | Backend + sidecar + broker containerized; Postgres deliberately left as a native service (see below) |
| Frontend | Leaflet + vanilla JS | No framework needed for a single live map page |

## The six build stages

1. **Static ingest** — Spring Boot + Postgres + BKK's static GTFS feed (stops, routes) → a working REST API.
2. **Live map** — real-time vehicle positions from BKK's FUTAR API, proxied server-side (API key never reaches the browser), rendered on Leaflet.
3. **Real-time ingestion pipeline** — a scheduled poller publishes every vehicle sighting through RabbitMQ to a Postgres-backed history table, running continuously.
4. **Delay-prediction sidecar** — a Python/FastAPI service that builds ground-truth delay labels from the accumulated history and serves live predictions.
5. **Feature engineering + external validation** — iterating on what actually predicts delay, and proving it against a real outside benchmark.
6. **Docker Compose** — the backend, sidecar, and broker all containerized with `restart: unless-stopped`, so the stack survives a reboot without manual relaunching.

## Engineering stories worth knowing (not just "it works")

### Building ground truth instead of trusting a vendor's prediction

The label isn't sourced from BKK's own `predictedArrivalTime` field. Instead: a vehicle reported `STOPPED_AT` a stop at 100% of the way there is treated as a real, physical "arrived here, now" event. That gets joined against the *static* GTFS schedule (on `trip_id` + `stop_sequence`) to compute `delay = actual_arrival − scheduled_arrival` — a ground-truth label built from raw telemetry, not relayed from someone else's black box.

### The walk-forward validation catch

The first model comparison used a single chronological 80/20 split and declared gradient-boosted trees the clear winner. Once more data accumulated, the *same* single-split approach suddenly showed GBT losing to a dumb per-route average — because the one split had, by chance, put a weekend-heavy period in training and a weekday-rush period in validation: two different traffic regimes. The fix was walk-forward validation (train on every earlier day, validate on each subsequent day in turn, average across all folds) — the same idea time-series backtesting uses, and it directly exposed *why* GBT was untrustworthy at low data volumes (a fold-by-fold train→validation gap of hundreds of seconds on thin-data days), not just *that* it scored worse on one lucky/unlucky split.

### The stale-schedule bug

Wiring the Spring Boot backend to actually call the prediction sidecar surfaced a live vehicle whose prediction inexplicably came back "unavailable." Root-caused rather than shrugged off: the downloaded static GTFS schedule was **9 days expired** (BKK's own file declared validity through Sep 3; the check happened Sep 12). The live-vs-static trip ID match rate showed the smoking gun — 73–77% through Aug 31, then a *cliff* to 25–43% starting exactly Sep 1, not gradually and not on the declared Sep 3 expiry date: the signature of a mid-season schedule swap (Hungarian schools start term Sep 1; BKK publishes a distinct school-year schedule), not gradual staleness. Re-downloading the current feed confirmed it: version dated literally the day of the check. This had been silently degrading training data for the prior week and a half, not just blocking the new feature — fixing it more than doubled the usable labeled dataset (2.6M → 6.1M rows).

### Feature engineering, one variable at a time

Rather than guessing at improvements, each one was measured in isolation against the walk-forward benchmark before moving to the next:

| Change | Result |
|---|---|
| Baseline (route/stop/time-of-day only) | ~94–104s MAE, and more data alone barely moved this ratio — a sign of a feature ceiling, not a data-volume problem |
| **Upstream delay** — this trip's own delay at its last observed stop | **~101s → ~58s MAE (≈45% reduction)** — delay propagates, and a vehicle's own recent state is far more informative than a historical average. Re-measured in the 2026-09-21 controlled run: removing it roughly doubles the error (~43s → ~89s), so it still holds up as the dominant signal |
| Weather (Open-Meteo, temperature/precipitation/wind) | **No measurable effect.** An earlier single run showed ~56s → ~53s, but a controlled re-run (same data, same folds, only weather switched off, paired confidence intervals) finds −0.1 s (95% CI −0.25 to −0.01 s) on the stable days. The earlier "gain" came from a few unstable early folds, where the model had trained on only 1–3 days (see the 2026-09-21 changelog entry) |
| Route-level live delay (for a trip's first observed stop, which has no upstream reading yet) | Closed the *coverage* gap (0% → 99.8% of previously-blind rows) but not the *accuracy* gap (still ~150s vs ~36s MAE on those rows). **Removed 2026-09-24:** a later controlled test found the model slightly *better* without it (−0.25s, 95% CI −0.33 to −0.16), and its live lookup didn't match how it was computed in training |
| `deviated` flag (BKK's own off-route indicator) | Real per-row signal, but only 0.09% of rows are ever flagged — too rare to move an aggregate metric |
| **The vehicle ahead** — the previous vehicle of the same line at the same stop: its delay, how long ago it passed, the timetable gap (2026-09-24) | **31.17s → 30.52s** (−0.65s, 95% CI −0.72 to −0.58), better on every one of 20 days |
| **Recent traffic on the stretch** — how much delay vehicles of *any* line gained between this stop and the one before it in the last 15 minutes (2026-09-26) | **30.51s → 29.85s** (−0.66s, 95% CI −0.70 to −0.61); aimed at congestion hotspots, where errors were twice the average |

Current production model (deployed 2026-09-26): **a linear model using the vehicle's own last delay, the vehicle ahead and recent traffic on the stretch — ~29.9s mean absolute error** in steady state (22 daily walk-forward folds from 2 Sep 2026, on the 25 Sep data; 30.5s without the stretch-traffic inputs; without the vehicle ahead as well, 31.2s on the 24 Sep data). For scale: carrying the current delay forward unchanged scores ~39s, the timetable alone ~122s, and gradient-boosted trees scored 36.1s on the 09-23 data. The linear model has been in production since 2026-09-23. It replaced gradient-boosted trees after first stops were relabelled by *departure* instead of arrival (see the [2026-09-23 changelog entry](CHANGELOG.md)) — a label fix, so the new figure is not directly comparable to the older ~44s below, which used the old first-stop label. Previously: **gradient-boosted trees, ~44s mean absolute error** in steady state (walk-forward validated, mean over 21 daily validation folds from 31 Aug 2026 onward, after the model has at least 2 days of training data), against ~109s for a naive per-route historical average. The older headline of ~53s averaged 17 daily folds *including* the two earliest ones (trained on 1 and 2 days, errors of 145s and 90s), which is why it reads higher; the baseline is ~110s either way. Retrained on fresher, larger data on 2026-09-22 (23 folds total, up from 13); the steady-state figure barely moved (43.2s → 43.9s), confirming it's stable rather than an early-data artifact. See the changelog for both re-runs.

### External validation: benchmarking against Google Maps

Beating an internal dumb baseline is a low bar. The real test: for the same real vehicle, right now, is this model's prediction closer to what actually happened than Google Maps' own live transit ETA? Building this surfaced three genuine measurement bugs, each one found by refusing to accept a suspicious number at face value:

1. Querying Google for a vehicle's *immediate* next stop returned no transit route at all — Google correctly judges that walking one more stop is often faster than waiting to reboard. Fixed by comparing against a stop several stops further down the same trip.
2. The first attempted fix for wildly-inflated Google numbers (480s, 840s+) was mathematically a no-op — confirmed by algebra, not just by the numbers staying identical. The real cause: querying from a live GPS point with `departureTime="now"` let Google assume a fresh rider who might board a materially *later* run of the same line — a different real-world service instance than the one being tracked. Fixed by anchoring the query to a stop the trip had *already departed*, with that specific trip's real departure time.
3. A smaller residual case: one sample's *scheduled* departure was 9 minutes *after* the query was made — a scheduled layover in the timetable, unrelated to when the real vehicle actually left. Fixed by preferring the real observed departure time (already being tracked) over the static schedule.

**Result, on 203 real reconciled comparisons:** this model's mean absolute error is **~66s**, versus **~327s** for Google's Routes API on the same trips — closer to the actual outcome in **69%** of head-to-head comparisons. (Google's Transit mode also has no way to request a specific route/line — confirmed against the actual API schema, not assumed — so match rate is inherently bounded by Budapest's route overlap; comparisons are kept strict, exact-route-only, since loosening them would mean validating against a different vehicle than the one whose real outcome is known.)

**Why this isn't really "beating Google":** Google's API is solving a different, harder problem — trip planning for a hypothetical fresh rider at any of thousands of agencies worldwide, without a live relationship to any one specific vehicle. This project tracks one specific transit network's live AVL feed at 15s resolution and knows exactly which physical vehicle is being asked about, its own recent delay, current weather, and the route's recent history — a narrower question with far more contextual signal. The honest framing is "a specialist system with dedicated telemetry for one network outperforms a generalist global router on that network's own vehicle-level predictions," not "beats Google Maps" as a general claim.

### A fairer comparison: BKK's own prediction — behind at random moments, ahead on equal information

Google's ETA answers a different, harder question than this project's, so beating it isn't a clean accuracy claim. BKK's own GTFS-RT TripUpdates feed makes a genuinely fair comparison possible instead: it publishes a `predictedArrivalTime` per stop for the *exact* trip being tracked, the same tripId/date this project already queries — no travel-time reframing needed, a direct delay-vs-delay comparison. Logged alongside every automatically-sampled prediction since 2026-09-21 and reconciled against real outcomes with the same rules as the live scoreboard (re-run 2026-09-24, reported per production model): **BKK's own predictor wins.** Previous gradient-boosted model, 10,060 comparisons: **48.8s vs BKK's 34.9s**, closer in 35%. First linear model (first 1,986 comparisons, evening and night): **52.5s vs BKK's 40.6s**, closer in 39% — BKK itself scored worse in those hours, so relative to BKK the gap narrowed (13.9s → 11.9s), but BKK is clearly ahead: ~90% of its predictions land within a minute vs ~78% of ours. With the vehicle-ahead inputs (2026-09-24) the gap narrowed again, 11.1s → 8.8s over the next 26 hours (82% of ours within a minute vs BKK's ~89%); the stretch-traffic model hasn't been compared against BKK yet. (An earlier figure of 55.9s vs 44.3s was inflated on both sides by scoring predictions for stops the vehicle had already reached — see the [2026-09-23 changelog entry](CHANGELOG.md).) Reported here as plainly as the Google win above: BKK's vendor system likely has operational signals this project doesn't have access to (dispatcher overrides, live traffic conditions), and a fair external benchmark that goes the other way is exactly the kind of result worth keeping visible, not just the flattering one.

**Then a stricter test went the other way (2026-09-28).** The comparison above samples vehicles at a random moment, often minutes after the last stop our prediction starts from, while BKK's prediction is likely updated from the vehicle's live position. To compare on equal information, for a random ~3% of the stop arrivals the every-stop job predicts, BKK is asked for the same next stop within seconds of our prediction (a median 5.7s *after* it, a slight edge to BKK). Over the first two days (Sunday and Monday, 17,151 pairs): **ours 23.9s vs BKK's 28.7s** (−4.8s, 95% interval −5.3 to −4.3), closer in 59%; **95.3% vs 92.8% accurate** by the MBTA standard; simply keeping the current delay scores 30.7s. It holds on both days and for every vehicle type (trams 15.7s vs 28.2s, buses 26.8s vs 28.7s), and at every horizon except under a minute, where BKK is 2s better. Both results stand: given the same information, this model predicts the next stop better than the operator's system; asked at a random moment between stops, BKK still wins. Two days is early. It gets re-read after a full week.

### Live error looked twice as bad as offline — it wasn't the model

For weeks the live scoreboard said ~50s while the offline test said ~30s, and the obvious reading was "the model does worse in real life". The fix was to measure live the way offline measures: a background job now predicts *every* vehicle's next stop the moment it leaves a stop and grades it on arrival. Measured that way, **live is 27.5s vs 28.4s offline** — no degradation. The old scoreboard had been grading a different population: clicked or randomly sampled vehicles, which include cold starts (no confirmed stop yet, ~232s), predictions made several stops ahead (~64s), and — the subtle one — vehicles picked at a random moment, which over-represents vehicles stuck on slow stretches, because a vehicle spends longer "in transit" exactly when it's delayed. That's the **inspection paradox** (the same reason you seem to always wait longer than the average bus gap): on the targets the sampler picked, the every-stop predictions scored 32s vs 24s elsewhere. The scoreboard now shows the every-stop figures, and "accurate" follows the MBTA's published standard (within ±1 min for predictions made under 3 minutes ahead, wider further out) instead of a home-made line.

### A live metric that quietly stopped working, and a wrong first fix along the way

The map's live accuracy panel disappeared. `EXPLAIN ANALYZE` on the reconciliation query showed why: `vehicle_position_snapshots` had grown to 132M rows, and the only index available (`trip_id` alone) wasn't selective enough anymore — each request pulled ~1,400 candidate rows off disk per logged prediction just to filter the rest by hand, 580K+ buffer reads, 52 seconds end to end. Fixed with a composite partial index built `CONCURRENTLY` against the live table (zero downtime, ingestion never paused) — 52s → 0.2s.

That surfaced a second, subtler problem: the live rolling MAE had jumped from ~36s to ~106s. First instinct was to reuse the outlier threshold already established in the training pipeline (`|delay| > 3600s`) — rebuilt, retested, and the number barely moved. **Wrong fix, caught by re-measuring instead of trusting the reasoning:** both actual outlier values (1906s, 2220s) were comfortably under that 3600s bound, which was calibrated for a multi-million-row training corpus, not a 50-sample live display. Re-diagnosed properly by pulling the full delay distribution across all reconciled predictions (p99 ≈ 807s, with a genuine gap before the next value at 1906s) and picking a threshold from that evidence instead of guessing twice. Confirmed via direct inspection of the raw snapshot timestamps that both flagged cases showed the concrete signature of a mismatched reconciliation (BKK reusing a `trip_id` for an unrelated dispatch), not a real model regression. MAE: 106s → 35.75s.

### A cross-language bug that looked like a networking mystery

Wiring the Java backend to call the Python sidecar over plain HTTP, every POST request came back corrupted — but only from Java, never from `curl`. Root cause, found in uvicorn's own log line ("Unsupported upgrade request") rather than guessed: Java's `RestClient` (backed by the JDK's `HttpClient`) was silently attempting an HTTP/2 cleartext ("h2c") upgrade against a plain HTTP service that only speaks HTTP/1.1 — something BKK's own HTTPS endpoint never triggered, since TLS negotiates the protocol version cleanly. Fixed by pinning the request factory to HTTP/1.1 explicitly. A reminder that "it works for the other service" doesn't mean the protocol assumptions are the same underneath.

### A label problem in 5% of rows that decided which model wins

Splitting the model's error by position in the trip showed one outlier group: a trip's **first stop** — 5% of rows, but ~183s error against ~35s everywhere else, adding ~9s to the overall figure on its own. The label was the culprit, not the model: delay was measured at the first "stopped at" sighting, and at a first stop vehicles turn up early and *wait* — so the label recorded when a bus arrived to wait (−236s on average, 90% "early"), not when the trip started. First, a guess was tested and rejected: that the model's "stop number" input was mostly learning this quirk (it wasn't — 1% of its value came from first stops). Then first stops were relabelled by **departure** (the last sighting there), the timetable needed no change (arrival = departure at every first stop), and every other label stayed bit-identical.

The retrain then did something unexpected: a **linear model beat gradient-boosted trees for the first time** (~32s vs ~36s). Rather than ship a surprise, it was checked on the same days with both label versions: old labels, trees win (43.8s vs 55.7s); new labels, linear wins (31.6s vs 35.9s). The bad first-stop labels had also poisoned the next stop's "own last delay" input (mean −179s), a kink only the trees could absorb. One more catch after deploying: the live scoreboard jumped from 53.8s to 65.8s because the old model's first-stop predictions were now being graded against departures they never predicted — fixed by judging every prediction against the event it was actually predicting.

### A red bus that was running on time: three bugs behind one symptom

A vehicle's popup said −9s, its marker said "Severe delay (6+ min)". Two hypotheses were tested against the data before touching code: a `trip_id` shared by two vehicles (query: zero cases) and inflation from vehicles standing at a stop (at midnight, long waits only showed up at first stops — looked ruled out). The real first cause was in the frontend: the map sent *one* service date — the first vehicle's — for every trip, and since BKK reuses trip IDs daily, just after midnight a bus could be colored from **yesterday's run of the same trip** (live check: 66 of 117 new-day vehicles affected; e.g. 34s real delay shown as 1317s). After the fix, the marker was *still* red in the browser — a cached old `app.js`, which led to serving static files with `Cache-Control: no-cache`.

Later that day the colors still looked off, so every live marker was compared against its own popup: 20% disagreed, and the big cases had one signature — the extra delay equalled the time since the vehicle reached its stop. The "standing still" theory had been right after all, just not visible at midnight: in the evening, buses parked at the end of the line still carry their last trip, and the color used the *latest* sighting, so it grew one second per second (a +3 min bus showed +61 min). Measuring at arrival instead cut red markers 33 → 7. The third cause was staleness — colors never expired — fixed with a cutoff that follows the timetable, so a night bus on a 30-minute stretch keeps its color while a bus heading to the depot goes grey. The lesson: a check that "rules out" a theory only rules it out for the moment it was run.

### Finding an undocumented API limit by testing it, not assuming it

The live map only ever showed traffic within a 6km radius — an arbitrary starting choice, never actually validated. Testing BKK's real-time API directly at increasing radii showed vehicle counts still climbing well past 6km (622 → 1,485 at 15km → 1,714 at 25km) — the original radius was badly under-covering the city. Kept probing until BKK's API returned `LIMIT_EXCEEDED` somewhere between 25km and 28km — a hard cap that isn't documented anywhere BKK publishes, confirmed as a real limit rather than rate-limiting by spacing out retries. Landed on 25km, safely under the cap — nearly 3x the live vehicle coverage, discovered by testing the actual system instead of trusting the original guess.

## Scale

- **186M+** vehicle position snapshots collected via the real-time ingestion pipeline (as of 2026-09-26), ~5,000 new ones a minute.
- **11.2M** labeled (features, delay) training rows built from that history (25 Sep build).
- **~250k** live predictions graded against reality per day by the every-stop job.
- Continuous operation since late August 2026, surviving multiple reboots via Docker's `restart: unless-stopped`.

## Data & license

Static schedule (GTFS) and real-time vehicle/trip/alert data are provided by BKK Zrt. under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.en) — free to use, share, and build on (including commercially or academically), with one condition: attribution.

> Data source: BKK Zrt., CC BY 4.0

If any part of this project (data, findings, or figures) is reused elsewhere — a thesis included — carry that same attribution line with it.

## Running it locally

```bash
# 1. Build the backend jar (host Maven, not a Docker build stage - see bkk-backend/Dockerfile)
cd bkk-backend && mvn -DskipTests package && cd ..

# 2. Have a trained model at bkk-delay-service/models/delay_model.joblib
#    (run bkk-delay-service/scripts/train_model.py first if not)

# 3. Set required env vars: BKK_API_KEY, DB_PASSWORD (native Postgres)

# 4. Bring up the backend + sidecar + broker
docker compose up --build -d
```

Postgres itself runs natively (a Windows/Linux service), not in Compose — see `docker-compose.yml`'s header comment for why: it already survives reboots on its own, and containerizing it would mean migrating tens of millions of real accumulated rows for a mostly cosmetic "one compose file" story.

**Keeping the timetable current.** BKK updates its GTFS timetable every few days; `bkk-delay-service/scripts/update_gtfs.py` installs new versions safely (checks, retired trips kept, restart, automatic rollback — see its docstring). On Windows, schedule it once from PowerShell:

```powershell
$action = New-ScheduledTaskAction -Execute "<repo>\bkk-delay-service\scripts\update_gtfs.cmd"
Register-ScheduledTask -TaskName "BKK timetable update" -Action $action `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 4:30am) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30))
```

## Known limitations

- Cold-start prediction (a trip's very first observed stop, ~7% of cases) is measurably worse than mid-trip prediction — a likely structural limit without a new data source (e.g. dispatch/shift-start data).
- Live accuracy matches offline once measured on the same population: every vehicle's next stop predicted from its previous confirmed stop (the `stop_predictions` job) scores **27.5s** live vs **28.4s** offline for the comparable group (2026-09-26, 247k predictions). The old sampled scoreboard's higher figure (~50s) came from *which* predictions it sampled — cold starts, predictions made several stops after the last confirmed one, and random-moment sampling that over-picks vehicles on slow stretches (an inspection paradox) — not from the model degrading live. Genuine weak spots: predictions several minutes ahead, departures from a first stop (~81s), buses (~32s) vs trams (~17s), night, and congestion hotspots. First stops were the weakest case until 2026-09-23 (vehicles arrive early and wait, so "arrival" there was poorly defined; now labelled by departure). Suburban-rail (HÉV) first stops still show 14-34% of departures more than a minute *early*, which trains don't do — likely the position feed dropping "stopped at platform" before the train leaves; ~15k rows (0.15%), left as is. Loses head-to-head against BKK's own GTFS-RT prediction when vehicles are sampled at a random moment (48.8s vs 34.9s for the gradient-boosted model, 52.5s vs 40.6s for the first linear one, gap down to 8.8s with the vehicle-ahead inputs), though it wins when both predict from the same moment (23.9s vs 28.7s, first two days); see "A fairer comparison".
- The model predicts a vehicle's *next* stop — typically ~1.5 minutes ahead. Further ahead the error grows steadily: carrying the current delay forward is off by ~49s at 3–6 minutes, ~67s at 6–12 and ~92s at 12–30 minutes ahead, and a first probe that told a model how far ahead it predicts gained only 5–8% on that (2026-09-27). That range is what riders mostly care about. A separate long-range model (2026-09-27) predicting 3, 5 and 10 stops ahead from what lies on the path scores 48.2s live vs 62.2s for keeping the current delay (first 1.5 days), but the map doesn't show those predictions yet.
- Arrival times are when our backend *saved* a position, not BKK's own timestamp — normally ~10s later, but if the ingestion queue falls behind, times shift. A scan of the whole history found one such episode (26 Sep, 16:26–16:58, up to ~19 min late); it is excluded from live figures and predates the current training data.
- No automated test suite yet.
- Volánbusz/MÁV-START vehicles that appear in BKK's live feed (~3%, a different agency) aren't matched against their own schedules — a legitimate low-priority backlog item, not a bug.

## Changelog

Notable fixes and changes, most recent first: see [CHANGELOG.md](CHANGELOG.md) (full history: `git log`).
