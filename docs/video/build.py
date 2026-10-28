#!/usr/bin/env python3
"""Builds the review cut of the tutorial video, from docs/VIDEO-SCRIPT.md and the recorder, with one command:

    python3 docs/video/build.py [--work DIR] [--name paybroker] [--deliver DIR]

Steps: 1 docs/video/narration.txt (written for listening; `## <scene id> <voice>` sections) is mirrored into the NARRATION column of the
scene table in VIDEO-SCRIPT.md (edit narration.txt, never the table); 2 videos/tts/narrate.py (Kokoro, local neural TTS; the voice
changes with the topic, as named in each section heading; shared + docs/video/pronounce.json respellings) speaks it: one wav per scene
plus narration.json (scene durations and the start/end of every sentence); 3 the recorder (docs/video/record.spec.ts) is driven by that
timing: each scene lasts audio + 0.7 s at least and each on-screen action starts when the sentence that illustrates it begins; it
starts a throwaway Postgres and a fresh PayBroker for every take and writes the scene start times; 4 ffmpeg mixes the narration at the
scene starts, encodes H.264 yuv420p + AAC and adds the captions (soft mov_text track in the mp4, and <name>.srt).

Needs: ffmpeg/ffprobe, Node 22+, Google Chrome, the repository .venv, Postgres 17 + pgvector binaries (on PATH, or PB_PG_BIN), and the Kokoro
tool in videos/tts (its own .venv and models; nothing is downloaded: override the folder with --tts-dir). See README.md.
Playwright's own ffmpeg build is not needed: a private PLAYWRIGHT_BROWSERS_PATH under --work holds a wrapper that runs the
system ffmpeg (no download). PB_PORT_RANGE (default 41700-41799) limits the ports the recorder uses.
Output: <work>/<name>.mp4, <name>.srt (and a copy in --deliver when given). docs/video/out/ is git-ignored.
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', 'VIDEO-SCRIPT.md')
NARRATION = os.path.join(HERE, 'narration.txt')
PRONOUNCE = os.path.join(HERE, 'pronounce.json')
VOICE = 'am_eric'  # default only: every section of narration.txt names its own voice
LEAD = 0.3  # silence before the voice in a scene (seconds); the pauses inside a scene are in narration.txt
TAIL = 0.4  # LEAD + audio + TAIL = the least a scene may last (audio + 0.7 s)
FFMPEG = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
FFPROBE = shutil.which('ffprobe') or '/opt/homebrew/bin/ffprobe'


def run(*a, **kw):
    return subprocess.run(a, check=True, **kw)


def narration_sections():
    """{scene id: text} from narration.txt (`## <id>` headings)."""
    parts = re.split(r'(?m)^##\s+(\S+)(?:[ \t]+\S+)?[ \t]*$', open(NARRATION, encoding='utf-8').read())
    return {sid: body.strip() for sid, body in zip(parts[1::2], parts[2::2])}


def table_cells():
    """{scene id: NARRATION cell text (without the quotes)} from the scene table of VIDEO-SCRIPT.md."""
    out = {}
    for line in open(SCRIPT, encoding='utf-8'):
        if not re.match(r'\|\s*\d+\s*\|', line):
            continue
        cells = [c.strip() for c in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
        m = re.match(r'^"(.*)"$', cells[4], re.S)
        out[cells[0]] = m.group(1) if m else cells[4]
    return out


def cell_text(section):
    """What the NARRATION column holds for a narration.txt section: paragraphs joined by spaces, explicit pauses kept, beats (...) dropped."""
    lines = [ln.strip() for ln in section.splitlines()]
    return re.sub(r'\s+', ' ', ' '.join(ln for ln in lines if ln and ln != '...')).strip()


def mirror_script():
    """Rewrite the NARRATION column of VIDEO-SCRIPT.md from narration.txt (table format kept): the script and the voice never diverge."""
    sections, seen, out = narration_sections(), set(), []
    for line in open(SCRIPT, encoding='utf-8').read().splitlines():
        m = re.match(r'^\|\s*(\d+)\s*\|', line)
        if m and m.group(1) in sections:
            c = re.split(r'(?<!\\)\|', line)
            if len(c) >= 7:
                c[4] = ' "' + cell_text(sections[m.group(1)]).replace('|', '\\|') + '" '
                line = '|'.join(c)
                seen.add(m.group(1))
        out.append(line)
    if set(sections) != seen:
        sys.exit(f'scene ids differ: narration.txt {list(sections)} vs script table {sorted(seen)}')
    open(SCRIPT, 'w', encoding='utf-8').write('\n'.join(out) + '\n')


def duration(path):
    return float(subprocess.check_output([FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path], text=True))


def make_audio(work, tts_dir, reuse):
    """narrate.py scenes ... -> one wav per scene + narration.json. Returns {id: {wav, dur, cues:[(start, end, sentence)]}}."""
    adir = os.path.join(work, 'audio')
    meta = os.path.join(adir, 'narration.json')
    if not (reuse and os.path.exists(meta)):
        os.makedirs(adir, exist_ok=True)
        py = os.path.join(tts_dir, '.venv', 'bin', 'python')
        run(py, os.path.join(tts_dir, 'narrate.py'), 'scenes', NARRATION, adir, '--voice', VOICE, '--pronounce', PRONOUNCE)  # per-scene voices come from the `## id voice` headings
    data = json.load(open(meta))
    return {sid: {'wav': os.path.join(adir, f'scene-{sid}.wav'), 'dur': v['duration'], 'cues': [(c['start'], c['end'], c['text']) for c in v['cues']]} for sid, v in data.items()}, meta


def srt_time(t):
    ms = round(t * 1000)
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def chunks(sentence, limit=84):
    """Splits a sentence into caption lines of at most `limit` characters, at word boundaries."""
    out, cur = [], ''
    for w in sentence.split():
        if cur and len(cur) + 1 + len(w) > limit:
            out.append(cur)
            cur = w
        else:
            cur = f'{cur} {w}'.strip()
    return out + [cur]


def write_srt(path, info, starts):
    n_cue, lines = 0, []
    for n in info:
        for a, b, s in info[n]['cues']:
            parts = chunks(s)
            total = sum(len(p) for p in parts)
            t = starts[n] + LEAD + a
            for p in parts:
                d = (b - a) * len(p) / total
                n_cue += 1
                lines.append(f'{n_cue}\n{srt_time(t)} --> {srt_time(t + d)}\n{p}\n')
                t += d
    open(path, 'w', encoding='utf-8').write('\n'.join(lines))


def browsers_path(work):
    """A private PLAYWRIGHT_BROWSERS_PATH: links to the installed browsers and an ffmpeg wrapper around the system ffmpeg."""
    rev = json.load(open(os.path.join(HERE, 'node_modules', 'playwright-core', 'browsers.json')))
    rev = next(b['revision'] for b in rev['browsers'] if b['name'] == 'ffmpeg')
    cache = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or os.path.expanduser('~/Library/Caches/ms-playwright')
    if os.path.exists(os.path.join(cache, f'ffmpeg-{rev}', 'ffmpeg-mac')):
        return cache
    bp = os.path.join(work, 'browsers')
    os.makedirs(os.path.join(bp, f'ffmpeg-{rev}'), exist_ok=True)
    for d in glob.glob(os.path.join(cache, '*')):
        link = os.path.join(bp, os.path.basename(d))
        if not os.path.lexists(link):
            os.symlink(d, link)
    wrapper = os.path.join(bp, f'ffmpeg-{rev}', 'ffmpeg-mac')
    open(wrapper, 'w').write(f'#!/bin/sh\nexec {FFMPEG} "$@"\n')
    os.chmod(wrapper, 0o755)
    for f in ('INSTALLATION_COMPLETE', 'DEPENDENCIES_VALIDATED'):
        open(os.path.join(bp, f'ffmpeg-{rev}', f), 'w').close()
    return bp


def video_offset(webm, card_at):
    """Seconds between the start of the webm and the recorder's clock zero: the title card (dark) first shows at card_at."""
    p = subprocess.run([FFMPEG, '-v', 'error', '-t', '8', '-i', webm, '-vf', 'fps=20,scale=64:36,signalstats,metadata=print:key=lavfi.signal_stats.YAVG:file=-', '-f', 'null', '-'], capture_output=True, text=True)
    t = None
    for line in p.stdout.splitlines():
        m = re.match(r'frame:\d+\s+pts:\d+\s+pts_time:([\d.]+)', line)
        if m:
            t = float(m.group(1))
        m = re.match(r'lavfi.signal_stats.YAVG=([\d.]+)', line)
        if m and t is not None and float(m.group(1)) < 80:  # the card is dark blue; the blank page is white
            return t - card_at
    return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', default=None, help='scratch directory (default: docs/video/out/build)')
    ap.add_argument('--name', default='paybroker')
    ap.add_argument('--deliver', default=None, help='also copy <name>.mp4 and <name>.srt here')
    ap.add_argument('--tts-dir', default=os.path.join(HERE, '..', '..', '..', 'videos', 'tts'), help='folder with narrate.py, its .venv and models')
    ap.add_argument('--reuse-audio', action='store_true', help='keep the narration of a previous run in --work/audio')
    ap.add_argument('--keep-recording', action='store_true', help='reuse the webm and scene times of a previous take in --work')
    a = ap.parse_args()
    work = os.path.abspath(a.work or os.path.join(HERE, 'out', 'build'))
    os.makedirs(work, exist_ok=True)

    if not os.path.isdir(os.path.join(HERE, 'node_modules')):
        run('npm', 'install', cwd=HERE)
    mirror_script()
    info, narration_json = make_audio(work, os.path.abspath(a.tts_dir), a.reuse_audio)
    print('audio seconds per scene:', {n: round(v['dur'], 1) for n, v in info.items()})

    rec = os.path.join(work, 'rec')
    times_json = os.path.join(work, 'scene-times.json')
    if not a.keep_recording:
        shutil.rmtree(rec, ignore_errors=True)
        env = dict(os.environ, NARRATION_JSON=narration_json, TIMES_JSON=times_json, PLAYWRIGHT_BROWSERS_PATH=browsers_path(work), PB_PORT_RANGE=os.environ.get('PB_PORT_RANGE', '41700-41799'), TMPDIR=os.path.join(work, 'tmp'))
        os.makedirs(env['TMPDIR'], exist_ok=True)
        run('npx', 'playwright', 'test', f'--output={rec}', cwd=HERE, env=env)
    webm = glob.glob(os.path.join(rec, '**', '*.webm'), recursive=True)
    if len(webm) != 1:
        sys.exit(f'expected one recording in {rec}, found {webm}')
    webm = webm[0]
    tm = json.load(open(times_json))
    off = video_offset(webm, tm['cardAt'])
    starts = {str(s['scene']): s['start'] for s in tm['scenes']}  # recorder clock; the video is cut `off` seconds in
    ends = {str(s['scene']): s['end'] for s in tm['scenes']}
    for n, v in info.items():
        room = ends[n] - starts[n] - LEAD - v['dur']
        print(f'scene {n}: {starts[n]:7.1f}s .. {ends[n]:7.1f}s  audio {v["dur"]:5.1f}s  room after voice {room:4.1f}s')
        if room < TAIL - 0.05:
            sys.exit(f'scene {n} is shorter than its narration + 0.7 s (lead + tail)')
    late = [x for x in tm.get('sync', []) if x['lateBy'] > 1.0]
    print(f'sync: {len(tm.get("sync", []))} cues, {len(late)} more than 1 s late', *[f"\n  scene {x['scene']} '{x['key']}' {x['lateBy']}s" for x in late])

    srt = os.path.join(work, a.name + '.srt')
    write_srt(srt, info, starts)
    cmd = [FFMPEG, '-v', 'error', '-y', '-ss', f'{max(0.0, off):.3f}', '-i', webm]
    for n in info:
        cmd += ['-i', info[n]['wav']]
    cmd += ['-i', srt]
    fc = ''.join(f'[{i}:a]adelay={round((starts[n] + LEAD) * 1000)}:all=1[a{i}];' for i, n in enumerate(info, 1))
    fc += ''.join(f'[a{i}]' for i in range(1, len(info) + 1)) + f'amix=inputs={len(info)}:normalize=0:duration=longest[aud0];[aud0]apad[aud]'
    end = ends[list(info)[-1]]
    final = os.path.join(work, a.name + '.mp4')
    cmd += ['-filter_complex', fc, '-map', '0:v', '-map', '[aud]', '-map', f'{len(info) + 1}:s',
            '-t', f'{end:.2f}', '-r', '25',
            '-vf', 'tpad=stop_mode=clone:stop_duration=6,scale=out_range=tv,format=yuv420p', '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-c:a', 'aac', '-b:a', '160k', '-ar', '44100',
            '-c:s', 'mov_text', '-metadata:s:s:0', 'language=eng', '-movflags', '+faststart', final]
    run(*cmd)
    print('final:', final, f'{duration(final):.1f}s')
    if a.deliver:
        os.makedirs(a.deliver, exist_ok=True)
        for f in (final, srt):
            shutil.copy(f, a.deliver)
        print('delivered to', a.deliver)


if __name__ == '__main__':
    main()
