#!/usr/bin/env python3
"""closr. puzzle builder. Standard library only.

Each day has six rounds per league. A round is a ranked stat list plus a target
rank; players name someone they think sits closest to that rank.

  python scripts/build.py                     # daily refresh (what the GitHub Action runs)
  python scripts/build.py --rebuild-static    # refetch past seasons and career lists now
  python scripts/build.py --today 2026-11-01  # build as if it were another day (testing)

data/static.json holds past-season lists, NBA all-time lists and NFL career totals
from 1999 up to the last completed season. It's rebuilt automatically every 30 days.
data/schedule.json holds the rounds for each day. Days already scheduled never change.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import math
import random
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'
RAW = DATA_DIR / 'raw'
INDEX = ROOT / 'index.html'
STATIC_FILE = DATA_DIR / 'static.json'
SCHEDULE_FILE = DATA_DIR / 'schedule.json'
START, END = '/*DATA:START*/', '/*DATA:END*/'

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
NFLVERSE = 'https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_reg_{y}.csv'
NFL_GAMES = 'https://github.com/nflverse/nfldata/raw/master/data/games.csv'
BBREF = 'https://www.basketball-reference.com/leagues/NBA_{y}_{page}.html'
BBREF_CAREER = 'https://www.basketball-reference.com/leaders/{stat}_career.html'

ROUNDS = 6
POOL_SEASON = 120          # players kept per season list (guesses outside score 0 anyway)
POOL_CAREER = 200
STATIC_MAX_AGE_DAYS = 30
SCHEDULE_KEEP_DAYS = 21
HORIZON_DAYS = 2           # schedule this many days past today (UTC)
NFL_MIN_GAMES = 3          # current season rounds start once teams have played this many games
NBA_MIN_GAMES = 12
FIRST_NFL_CAREER_SEASON = 1999
LEAGUES = ('nfl', 'nba')


def log(*a):
    print(*a, file=sys.stderr)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- fetching

_last_bbref = [0.0]


def fetch(url, name, max_age_hours=6.0):
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / name
    if path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        return path.read_text(encoding='utf-8')
    if 'basketball-reference' in url:  # stay well under their 20 requests/minute limit
        wait = 4 - (time.time() - _last_bbref[0])
        if wait > 0:
            time.sleep(wait)
        _last_bbref[0] = time.time()
    log('fetch', url)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode('utf-8')
    path.write_text(body, encoding='utf-8')
    return body


def bbref_table(doc, table_id):
    doc = doc.replace('<!--', '').replace('-->', '')
    m = re.search(r'<table[^>]*id="%s".*?</table>' % table_id, doc, re.S)
    if not m:
        return []
    rows = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(0), re.S):
        rows.append({stat: html.unescape(re.sub(r'<[^>]+>', '', val)).strip()
                     for stat, val in re.findall(r'<t[dh][^>]*data-stat="([^"]+)"[^>]*>(.*?)</t[dh]>', tr, re.S)})
    return rows


# ---------------------------------------------------------------- teams

NFL_TEAMS = {'ARI': 'Cardinals', 'ATL': 'Falcons', 'BAL': 'Ravens', 'BUF': 'Bills', 'CAR': 'Panthers', 'CHI': 'Bears',
             'CIN': 'Bengals', 'CLE': 'Browns', 'DAL': 'Cowboys', 'DEN': 'Broncos', 'DET': 'Lions', 'GB': 'Packers',
             'HOU': 'Texans', 'IND': 'Colts', 'JAX': 'Jaguars', 'KC': 'Chiefs', 'LA': 'Rams', 'LAR': 'Rams', 'LAC': 'Chargers',
             'LV': 'Raiders', 'OAK': 'Raiders', 'SD': 'Chargers', 'STL': 'Rams', 'MIA': 'Dolphins', 'MIN': 'Vikings',
             'NE': 'Patriots', 'NO': 'Saints', 'NYG': 'Giants', 'NYJ': 'Jets', 'PHI': 'Eagles', 'PIT': 'Steelers',
             'SEA': 'Seahawks', 'SF': '49ers', 'TB': 'Buccaneers', 'TEN': 'Titans', 'WAS': 'Commanders'}
NBA_TEAMS = {'ATL': 'Hawks', 'BOS': 'Celtics', 'BRK': 'Nets', 'CHO': 'Hornets', 'CHI': 'Bulls', 'CLE': 'Cavaliers',
             'DAL': 'Mavericks', 'DEN': 'Nuggets', 'DET': 'Pistons', 'GSW': 'Warriors', 'HOU': 'Rockets', 'IND': 'Pacers',
             'LAC': 'Clippers', 'LAL': 'Lakers', 'MEM': 'Grizzlies', 'MIA': 'Heat', 'MIL': 'Bucks', 'MIN': 'Timberwolves',
             'NOP': 'Pelicans', 'NYK': 'Knicks', 'OKC': 'Thunder', 'ORL': 'Magic', 'PHI': '76ers', 'PHO': 'Suns',
             'POR': 'Trail Blazers', 'SAC': 'Kings', 'SAS': 'Spurs', 'TOR': 'Raptors', 'UTA': 'Jazz', 'WAS': 'Wizards'}


def nba_team(abbr):
    if abbr in NBA_TEAMS:
        return NBA_TEAMS[abbr]
    return f'{abbr[0]} teams' if abbr.endswith('TM') else abbr


# ---------------------------------------------------------------- stats

def stat(field):
    return lambda r: num(r.get(field))


NFL_STATS = {
    'pass_yds': dict(t='Passing yards', col='passing_yards', u='yds'),
    'pass_td':  dict(t='Passing touchdowns', col='passing_tds', u='TD'),
    'rush_yds': dict(t='Rushing yards', col='rushing_yards', u='yds'),
    'rush_td':  dict(t='Rushing touchdowns', col='rushing_tds', u='TD'),
    'rec_yds':  dict(t='Receiving yards', col='receiving_yards', u='yds'),
    'rec':      dict(t='Receptions', col='receptions', u='rec'),
    'rec_td':   dict(t='Receiving touchdowns', col='receiving_tds', u='TD'),
    'sacks':    dict(t='Sacks', col='def_sacks', u='sacks'),
    'scrim':    dict(t='Scrimmage yards', cols=('rushing_yards', 'receiving_yards'), u='yds', season_only=True,
                     note='Rushing plus receiving yards.'),
}
NFL_CAREER_COLS = sorted({s['col'] for s in NFL_STATS.values() if 'col' in s})

NBA_STATS = {
    'pts': dict(t='Points', col='pts', u='pts'),
    'trb': dict(t='Rebounds', col='trb', u='reb'),
    'ast': dict(t='Assists', col='ast', u='ast'),
    'stl': dict(t='Steals', col='stl', u='stl'),
    'blk': dict(t='Blocks', col='blk', u='blk'),
    'fg3': dict(t='Three-pointers made', col='fg3', u='3PM'),
    'ppg': dict(t='Points per game', per='pts', u='ppg', f='dec1'),
    'rpg': dict(t='Rebounds per game', per='trb', u='rpg', f='dec1'),
    'apg': dict(t='Assists per game', per='ast', u='apg', f='dec1'),
}
NBA_CAREER = {'pts': 'pts', 'trb': 'trb', 'ast': 'ast', 'stl': 'stl', 'blk': 'blk', 'fg3': 'fg3'}
NBA_CAREER_NOTES = {'stl': 'Steals were first counted in 1973–74.', 'blk': 'Blocks were first counted in 1973–74.',
                    'fg3': 'The three-point line arrived in 1979–80.'}


def ranked(entries, size):
    entries = [e for e in entries if e[2] > 0]
    entries.sort(key=lambda e: (-e[2], e[0]))
    return [[n, s, round(v, 4)] for n, s, v in entries[:size]]


# ---------------------------------------------------------------- seasons

def nfl_rows(season, max_age):
    doc = fetch(NFLVERSE.format(y=season), f'nfl_reg_{season}.csv', max_age)
    return [r for r in csv.DictReader(io.StringIO(doc)) if r.get('player_display_name')]


def nfl_progress(season, max_age):
    games = csv.DictReader(io.StringIO(fetch(NFL_GAMES, 'nfl_games.csv', max_age)))
    played, week = {}, 0
    for g in games:
        if g['season'] != str(season) or g['game_type'] != 'REG' or g['home_score'] == '':
            continue
        week = max(week, int(g['week']))
        for t in (g['home_team'], g['away_team']):
            played[t] = played.get(t, 0) + 1
    gp = statistics.median(played.values()) if played else 0
    return week, gp, week >= 18 and gp >= 17


def nfl_season_pools(rows):
    pools = {}
    for kind, spec in NFL_STATS.items():
        cols = spec.get('cols') or (spec['col'],)
        pools[kind] = ranked([(r['player_display_name'], f"{r['position']} · {NFL_TEAMS.get(r['recent_team'], r['recent_team'])}",
                               sum(num(r[c]) for c in cols)) for r in rows], POOL_SEASON)
    return pools


def nba_season(end_year, max_age):
    tot = bbref_table(fetch(BBREF.format(y=end_year, page='totals'), f'bbr_{end_year}_totals.html', max_age), 'totals_stats')
    players, seen = [], set()
    for r in tot:
        name = r.get('name_display')
        if not name or name in seen or name in ('Player', 'League Average'):
            continue
        seen.add(name)  # traded players: Basketball Reference lists the combined row first
        players.append(r)
    return players


def nba_progress(end_year, max_age):
    std = bbref_table(fetch(BBREF.format(y=end_year, page='standings'), f'bbr_{end_year}_standings.html', max_age), 'expanded_standings')
    gp = [sum(map(int, r['Overall'].split('-'))) for r in std if r.get('ranker', '').isdigit() and '-' in r.get('Overall', '')]
    gp = statistics.median(gp) if gp else 0
    return gp, gp >= 82


def nba_season_pools(players, team_gp, complete):
    min_games = 58 if complete else max(1, math.ceil(0.7 * team_gp))
    pools = {}
    for kind, spec in NBA_STATS.items():
        out = []
        for r in players:
            g = num(r['games'])
            if spec.get('per'):
                if g < min_games:
                    continue
                v = num(r[spec['per']]) / g
            else:
                v = num(r[spec['col']])
            out.append((r['name_display'], f"{r['pos']} · {nba_team(r['team_name_abbr'])}", v))
        pools[kind] = ranked(out, POOL_SEASON)
    return pools, min_games


def nba_label(end_year):
    return f'{end_year - 1}–{str(end_year)[2:]}'


def calendar(today):
    """Which seasons are current, and which are the last three completed."""
    nfl_current = today.year if today.month >= 9 else (today.year - 1 if today.month == 1 else None)
    nfl_last = today.year - 1 if today.month >= 2 else today.year - 2
    nba_current = today.year + 1 if today.month >= 10 else (today.year if today.month <= 4 else None)
    nba_last = today.year if today.month >= 5 else today.year - 1
    return {'nfl': (nfl_current, [nfl_last - 2, nfl_last - 1, nfl_last]),
            'nba': (nba_current, [nba_last - 2, nba_last - 1, nba_last])}


# ---------------------------------------------------------------- static data

def build_static(today):
    cal = calendar(today)
    long = 24 * 365 * 10   # completed seasons don't change
    static = {'built': today.isoformat(), 'nfl': {'seasons': {}}, 'nba': {'seasons': {}}}

    nfl_last = cal['nfl'][1][-1]
    base = {}
    for y in range(FIRST_NFL_CAREER_SEASON, nfl_last + 1):
        rows = nfl_rows(y, long)
        if y in cal['nfl'][1]:
            static['nfl']['seasons'][str(y)] = nfl_season_pools(rows)
        for r in rows:
            p = base.setdefault(r['player_id'], {'name': r['player_display_name'], 'v': [0.0] * len(NFL_CAREER_COLS)})
            p['name'], p['pos'], p['team'], p['last'] = r['player_display_name'], r['position'], r['recent_team'], y
            for i, c in enumerate(NFL_CAREER_COLS):
                p['v'][i] += num(r[c])
    static['nfl']['career_cols'] = NFL_CAREER_COLS
    static['nfl']['career_through'] = nfl_last
    static['nfl']['career_base'] = {pid: [p['name'], p['pos'], p['team'], p['last'], [round(x, 1) for x in p['v']]]
                                    for pid, p in base.items() if p['last'] >= nfl_last - 6 and any(p['v'])}

    for y in cal['nba'][1]:
        players = nba_season(y, long)
        gp, complete = nba_progress(y, long)
        static['nba']['seasons'][str(y)] = nba_season_pools(players, gp, True)[0]
    alltime = {}
    for kind, s in NBA_CAREER.items():
        doc = fetch(BBREF_CAREER.format(stat=s), f'bbr_career_{s}.html', 24 * 7).replace('<!--', '').replace('-->', '')
        m = re.search(r'<table[^>]*id="nba".*?</table>', doc, re.S)
        out = []
        for tr in re.findall(r'<tr>(.*?)</tr>', m.group(0) if m else '', re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
            if len(cells) < 3:
                continue
            raw_name = cells[1]
            name = html.unescape(re.sub(r'<[^>]+>', '', raw_name)).replace('*', '').strip()
            status = 'Active' if '<strong>' in raw_name else 'Hall of Fame' if '*' in raw_name else 'Retired'
            out.append((name, status, num(re.sub(r'[^0-9.]', '', cells[2]))))
        alltime[kind] = ranked(out, POOL_CAREER)
        log('nba all-time', kind, len(alltime[kind]))
    static['nba']['alltime'] = alltime
    static['nba']['alltime_date'] = today.isoformat()
    return static


# ---------------------------------------------------------------- scopes

def scopes_for(today, static):
    """Every list a round can draw from, grouped by scope, for each league."""
    cal = calendar(today)
    out = {}

    # NFL
    nfl = {}
    cur_season, past = cal['nfl']
    for y in past:
        nfl[f's{y}'] = {'label': f'{y} season', 'note': f'{y} regular season.', 'pools': static['nfl']['seasons'][str(y)], 'role': 'past'}
    nfl[f's{past[-1]}']['role'] = 'last'
    cur_rows, week = None, 0
    if cur_season:
        try:
            week, gp, complete = nfl_progress(cur_season, 6)
            if gp >= NFL_MIN_GAMES:
                cur_rows = nfl_rows(cur_season, 6)
                label = f'{cur_season} season' if complete else f'{cur_season} season so far'
                note = f'{cur_season} regular season.' if complete else f'{cur_season} regular season through Week {week}.'
                nfl['cur'] = {'label': label, 'note': note, 'pools': nfl_season_pools(cur_rows), 'role': 'cur'}
            log('nfl', cur_season, 'week', week, 'ready' if 'cur' in nfl else 'not enough games yet')
        except Exception as e:  # network or format problems: skip current-season rounds today
            log('nfl current season unavailable:', e)
    cols = static['nfl']['career_cols']
    base = static['nfl']['career_base']
    career = {}
    if cur_rows:
        active_season = cur_season
        for r in cur_rows:
            b = base.get(r['player_id'])
            v = [x + num(r[c]) for x, c in zip(b[4] if b else [0.0] * len(cols), cols)]
            career[r['player_id']] = (r['player_display_name'], r['position'], r['recent_team'], v)
        through = f' Through Week {week} of {cur_season}.'
    else:
        active_season = static['nfl']['career_through']
        for pid, (name, pos, team, last, v) in base.items():
            if last == active_season:
                career[pid] = (name, pos, team, v)
        through = f' Through the {active_season} season.'
    pools = {}
    for kind, spec in NFL_STATS.items():
        if spec.get('season_only'):
            continue
        i = cols.index(spec['col'])
        pools[kind] = ranked([(n, f'{pos} · {NFL_TEAMS.get(t, t)}', v[i]) for n, pos, t, v in career.values()], POOL_CAREER)
    nfl['career'] = {'label': 'Career, active players', 'role': 'career', 'pools': pools,
                     'note': f'Regular-season career totals for players who have played in {active_season}.{through}'}
    out['nfl'] = nfl

    # NBA
    nba = {}
    cur_season, past = cal['nba']
    for y in past:
        nba[f's{y}'] = {'label': f'{nba_label(y)} season', 'note': f'{nba_label(y)} regular season.',
                        'pools': static['nba']['seasons'][str(y)], 'role': 'past'}
    nba[f's{past[-1]}']['role'] = 'last'
    if cur_season:
        try:
            gp, complete = nba_progress(cur_season, 6)
            if gp >= NBA_MIN_GAMES:
                pools, min_games = nba_season_pools(nba_season(cur_season, 6), gp, complete)
                d = today - dt.timedelta(days=1)
                lab = nba_label(cur_season)
                nba['cur'] = {'label': f'{lab} season' + ('' if complete else ' so far'), 'role': 'cur', 'pools': pools,
                              'note': f'{lab} regular season' + ('.' if complete else f', games through {d:%b} {d.day}.'),
                              'min_games': min_games}
            log('nba', nba_label(cur_season), 'median games', gp, 'ready' if 'cur' in nba else 'not enough games yet')
        except Exception as e:
            log('nba current season unavailable:', e)
    d = dt.date.fromisoformat(static['nba']['alltime_date'])
    nba['alltime'] = {'label': 'All-time', 'role': 'career', 'pools': static['nba']['alltime'],
                      'note': f'NBA regular season, all time. ABA stats not included. Updated {d:%b} {d.day}, {d.year}.'}
    out['nba'] = nba
    return out


# ---------------------------------------------------------------- rounds

def tie_block(pool, i):
    v = pool[i][2]
    lo = i
    while lo > 0 and pool[lo - 1][2] == v:
        lo -= 1
    hi = i
    while hi + 1 < len(pool) and pool[hi + 1][2] == v:
        hi += 1
    return lo, hi


def pick_target(rng, pool, lo, hi):
    hi = min(hi, len(pool) - 10)
    if hi < lo:
        return None
    good = []
    for t in range(lo, hi + 1):
        a, b = tie_block(pool, t - 1)
        if b - a <= 1:  # at most two players share the target's value
            good.append(t)
    return rng.choice(good) if good else None


TARGETS = {'cur': (6, 30), 'last': (8, 40), 'past': (8, 40), 'career': (8, 45)}


def make_round(lg, date, scope_key, scope, kind, rng):
    pool = scope['pools'].get(kind)
    if not pool:
        return None
    lo, hi = TARGETS[scope['role']]
    if lg == 'nba' and scope_key == 'alltime':
        lo, hi = 10, 100
    target = pick_target(rng, pool, lo, hi)
    if not target:
        return None
    spec = (NFL_STATS if lg == 'nfl' else NBA_STATS)[kind]
    note = scope['note']
    if spec.get('note'):
        note += ' ' + spec['note']
    if spec.get('per'):
        note += f" Minimum {scope.get('min_games', 58)} games."
    if lg == 'nba' and scope_key == 'alltime' and kind in NBA_CAREER_NOTES:
        note += ' ' + NBA_CAREER_NOTES[kind]
    return {'id': f'{lg}-{date}-{scope_key}-{kind}', 'k': kind, 'sc': scope_key, 't': spec['t'], 's': scope['label'],
            'n': note, 'target': target, 'u': spec['u'], 'f': spec.get('f', 'num'), 'pool': pool}


def plan(scopes, rng):
    roles = {}
    for key, s in scopes.items():
        roles.setdefault(s['role'], []).append(key)
    career = roles['career'][0]
    last = roles['last'][0]
    older = [k for k in scopes if scopes[k]['role'] in ('past', 'last')]
    if 'cur' in scopes:
        keys = ['cur', 'cur', 'cur', career, rng.choice(older), rng.choice(older)]
    else:
        keys = [last, last, career, career, rng.choice(roles.get('past', [last])), rng.choice(roles.get('past', [last]))]
    rng.shuffle(keys)
    return keys


def rounds_for(lg, date, scopes, schedule):
    key = date.isoformat()
    rng = random.Random(f'closr:{lg}:{key}')
    recent = {(r['sc'], r['k']) for d in sorted(schedule) if d < key for r in schedule[d].get(lg, [])[-ROUNDS:]}
    used, rounds = set(), []
    for sc in plan(scopes, rng):
        kinds = [k for k in scopes[sc]['pools'] if k not in used]
        rng.shuffle(kinds)
        kinds.sort(key=lambda k: (sc, k) in recent)
        for k in kinds:
            r = make_round(lg, key, sc, scopes[sc], k, rng)
            if r:
                used.add(k)
                rounds.append(r)
                break
    if len(rounds) < ROUNDS:
        raise SystemExit(f'could only build {len(rounds)} rounds for {lg} {key}')
    return rounds


def embed(schedule, today):
    keep = [(today + dt.timedelta(days=d)).isoformat() for d in (-1, 0, 1)]
    payload = {'updated': today.isoformat(), 'days': {d: schedule[d] for d in keep if d in schedule}}
    js = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    page = INDEX.read_text(encoding='utf-8')
    a, b = page.index(START) + len(START), page.index(END)
    INDEX.write_text(page[:a] + js + page[b:], encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--rebuild-static', action='store_true')
    ap.add_argument('--today', type=dt.date.fromisoformat)
    args = ap.parse_args()
    today = args.today or dt.datetime.now(dt.timezone.utc).date()

    static = json.loads(STATIC_FILE.read_text(encoding='utf-8')) if STATIC_FILE.exists() else None
    if args.rebuild_static or not static or (today - dt.date.fromisoformat(static['built'])).days >= STATIC_MAX_AGE_DAYS:
        static = build_static(today)
        STATIC_FILE.write_text(json.dumps(static, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        log('static data rebuilt')
    schedule = json.loads(SCHEDULE_FILE.read_text(encoding='utf-8')) if SCHEDULE_FILE.exists() else {}

    dates = [today + dt.timedelta(days=d) for d in range(-1, HORIZON_DAYS + 1)]
    missing = [(d, lg) for d in dates for lg in LEAGUES if lg not in schedule.get(d.isoformat(), {})]
    if missing:
        scopes = scopes_for(today, static)
        for d, lg in missing:
            schedule.setdefault(d.isoformat(), {})[lg] = rounds_for(lg, d, scopes[lg], schedule)
            log(d, lg, ' | '.join(f"{r['t']} ({r['s']}) #{r['target']}" for r in schedule[d.isoformat()][lg]))
    cutoff = (today - dt.timedelta(days=SCHEDULE_KEEP_DAYS)).isoformat()
    schedule = {d: v for d, v in sorted(schedule.items()) if d >= cutoff}
    SCHEDULE_FILE.write_text(json.dumps(schedule, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    embed(schedule, today)
    log('index.html updated for', today)


if __name__ == '__main__':
    main()
