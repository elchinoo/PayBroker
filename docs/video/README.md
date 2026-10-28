# Tutorial video: recorder and build

One command goes from `docs/VIDEO-SCRIPT.md` and the recorder to the finished review cut:

```bash
cd paybroker                      # needs .venv (uv venv && uv pip install -e ".[dev]") and Postgres 17 + pgvector on PATH or PB_PG_BIN
python3 docs/video/build.py --work /some/scratch/dir --name paybroker --deliver /where/to/copy
```

Output: `<work>/paybroker.mp4` (1920x1080, H.264 yuv420p, AAC, faststart, captions as a soft `mov_text` track) and
`<work>/paybroker.srt`. Everything stays local: a throwaway Postgres on 127.0.0.1 and PayBroker on a free port (from
`PB_PORT_RANGE`, default `41700-41799`; the shop stand-in and the webhook receiver use ports from the same range), with no AI
account (the built-in rule-based triage writes the case summary). **A fresh database on every take**: the velocity rules, the
blocklist and idempotency keys all depend on an empty database, so nothing is reused. The admin password is random per take and is
never written anywhere. `out/` is git-ignored; do not commit media.

What it does:

1. Mirrors `narration.txt` (the narration, written for listening) into the NARRATION column of the scene table in
   `VIDEO-SCRIPT.md`. Edit `narration.txt`, never the table: the next build overwrites it (a paragraph break is a space, a `...`
   beat is dropped, a `[pause 1.5]` stays).
2. Speaks `narration.txt` with `videos/tts/narrate.py scenes` (Kokoro, a local neural voice: nothing leaves the machine, nothing is
   downloaded). The voice changes with the topic: the heading of every section names it, `## 3 af_heart`. Output: one wav per
   scene and `narration.json` with each scene's duration and the start and end of every sentence. Respellings for the voice live in
   `pronounce.json`, on top of the shared `videos/tts/pronounce.json`; captions keep the written form. Prefer rewording a sentence
   to a fragile respelling.
3. Runs `record.spec.ts` (Playwright, real Chrome) with `NARRATION_JSON`. Every scene lasts at least 0.3 s lead + narration + 0.4 s
   tail (audio + 0.7 s). Each on-screen action waits for the sentence that illustrates it (`await at(page, 'words of the sentence')`),
   so a click, a callout or a page change starts as that sentence begins; if a page is slow the action runs at once and the delay is
   logged. The recorder writes the scene start times (`TIMES_JSON`, with a `sync` list of cue time vs actual time).
4. ffmpeg places each scene's audio at its start time, encodes, and adds the captions. The `.srt` comes from the per-sentence
   times of `narration.json`.
5. It stops with an error if any scene is shorter than its narration + 0.7 s.

Structure (problem first, about 8 minutes): 1 the problem and the three-colour diagram (PLATFORM = Postgres on AgentPG and its EDB extensions,
REFERENCE SCAFFOLDING = sign-in, model gateway, secret vault, SAMPLE APP = PayBroker; the legend returns in the end card), 2 to 8 the core flow
(setup, merchant, authorize, 3-D Secure, cascade, risky payment and case, webhooks), 9 to 13 **under the hood** (pgvector HNSW nearest cases, the
fuzzystrmatch `NAME_MISMATCH` signal and the four built-in rules, masked views for a read-only role, Database insights), 14 try it yourself.
Every under-the-hood scene opens with the customer's problem ("I am trying to X"), then shows the real SQL and the real output.

Real versus captured: everything is a real run against the throwaway local Postgres (the recorder creates the `a1p_readonly` group role and the
login role `reporting_reader` before `paybroker migrate`, to mimic what AgentPG provides, and preloads `pg_stat_statements`), except the two
terminal cards in scene 13, which print the PayBroker blocks of `videos/live-captures/extensions-installed.txt` and `ecoctl-insights.txt` under a
banner *CAPTURED from our live AgentPG fleet: not run in this video* (the instance id is dropped). The recorder fails if a capture block is missing.
Local Postgres cannot show `edb_stat_monitor`, `edb_pg_tuner` or `query_advisor`; the narration says so.

Voice plan (one accent, US; never the same voice in two consecutive topics, male and female alternate):

| Topic | Scenes | Voice |
|---|---|---|
| The problem, setup, the dashboard | 1, 2 | `am_eric` |
| Merchant, authorize, the payment page | 3, 4, 5 | `af_heart` |
| Test cards | 6 | `am_michael` |
| The risky payment and its case | 7 | `af_bella` |
| Webhooks | 8 | `am_adam` |
| Nearest cases (pgvector) | 9 | `af_sarah` |
| Name check and the rules | 10, 11 | `am_michael` |
| Masking | 12 | `af_bella` |
| Database insights | 13 | `am_adam` |
| Try it yourself | 14 | `af_heart` |

Writing narration: one `## <scene id> <voice>` section per scene; short sentences, one idea each; a blank line is a pause, `...` a
short beat, `[pause 1.5]` a deliberate long pause where something happens on screen that the viewer should watch (a payment being
scored, a redirect): do not talk over a wait. Never change voice inside a scene. If you change a sentence the recorder anchors an
action to, update the matching `at(...)` text in `record.spec.ts` (it fails loudly when no sentence contains the text).

The recorder asserts what the script claims (`403` with `NEW_ACCOUNT`, `402 CARD_DECLINED`, `REQUIRES_ACTION` then the signed
return, routing `error` then `approved`, `signature ok` for the three event types, `delivered` events), so a take that drifts from
the script fails instead of recording something false. Keys and the signing secret are blurred the moment they are rendered and
masked a second later; the terminal pages never print a key (the commands use shell variables).

Options: `--keep-recording` re-mixes a previous take in `--work`; `--reuse-audio` keeps the narration already spoken in
`--work/audio`; `--tts-dir` points to another Kokoro tool folder (default `../../videos/tts` relative to the repository);
`PACE=0.3` shortens the recorder's own waits (a rehearsal, not for the final cut; run it without `NARRATION_JSON` it does not wait for the sentences); `REC_SHOTS=<dir>` saves a screenshot at every
callout (a quick visual check); `PB_PORT_RANGE` limits the
ports; `HEADED=1` shows the browser. Rehearsal of the actions only: `PACE=0.3 REC_VIDEO=off npx playwright test` in this folder.

Playwright's own ffmpeg build is not required: when `ffmpeg-<rev>` is missing from the Playwright browser cache, `build.py`
creates a private `PLAYWRIGHT_BROWSERS_PATH` under `--work` (links to the installed browsers plus a wrapper that runs the
system `ffmpeg`). Nothing is downloaded. Needs: macOS, `ffmpeg`, Node 22+, Google Chrome, `npm install` in this folder, and the
Kokoro tool folder with its own `.venv` and models (`videos/tts`).

The terminal scenes (4, 6, 7, 8, 9, 10, 12, 13) are drawn as a terminal page in the same recording, with the real API answers and real `psql` output (a top bar tags what is PLATFORM, SAMPLE APP or CAPTURED). The setup code
visible in scene 1 belongs to the throwaway server and is already used up when the video ends.
