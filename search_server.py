#!/usr/bin/env python3
"""
YouTube Trending Search Server — run: python3 search_server.py
"""
import json, os, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from collections import Counter

PORT = 8765
DATA_FILE = os.path.join(os.path.dirname(__file__), 'ytdata.json')

print("Loading data...", end='', flush=True)
with open(DATA_FILE, 'r', encoding='utf-8') as f:
    RAW = json.load(f)
VIDEOS   = RAW['videos']
CHANNELS = RAW['channels']
print(f" {len(VIDEOS):,} videos, {len(CHANNELS):,} channels loaded.")

# Build global tag frequency index
print("Building tag index...", end='', flush=True)
from collections import Counter as _Counter
_tc = _Counter()
for _v in VIDEOS:
    _tg = _v.get('tg','')
    if _tg and _tg != '[none]':
        for _t in _tg.split('|'):
            _t = _t.strip().lower()
            if len(_t) > 2:
                _tc[_t] += 1
GLOBAL_TAG_FREQ = dict(_tc.most_common(5000))  # top 5000 tags
GLOBAL_TOTAL_VIDEOS = len(VIDEOS)
print(f" {len(GLOBAL_TAG_FREQ):,} unique tags indexed.")

# Pre-compute alltime cache so /alltime responds instantly
print("Pre-computing all-time charts...", end='', flush=True)
def _controversy_sort(v):
    views = v.get('v', 1)
    lr = v.get('l', 0) / views
    cr = v.get('c', 0) / views
    return (cr / max(lr, 0.0001)) * (views ** 0.25)

_ALLTIME_POPULAR = sorted(VIDEOS, key=lambda x: x.get('v', 0), reverse=True)[:50]
_con_vids = [v for v in VIDEOS if (
    v.get('v',0) > 0 and v.get('c',0) > 0 and
    (v.get('l',0)/v.get('v',1) < 0.05 or v.get('c',0)/v.get('v',1) < 0.005)
)]
_ALLTIME_CONTROVERSIAL = sorted(_con_vids, key=_controversy_sort, reverse=True)[:50]
_ALLTIME_TOP_TAGS = [{'tag': t, 'count': c} for t, c in _tc.most_common(25)]
_ALLTIME_CACHE = {
    'total_matches': len(VIDEOS),
    'popular': _ALLTIME_POPULAR,
    'controversial': _ALLTIME_CONTROVERSIAL,
    'by_country': {},
    'channel_only': [],
    'top_tags': _ALLTIME_TOP_TAGS,
    'is_alltime': True,
}
print(" done.")

COUNTRIES = ['BR','CA','DE','FR','GB','IN','JP','KR','MX','RU','US']

def word_match(text, keyword):
    pattern = r'(?<![a-z0-9])' + re.escape(keyword) + r'(?![a-z0-9])'
    return bool(re.search(pattern, text.lower()))

def matches_any(text, keywords):
    t = text.lower()
    return any(word_match(t, kw) for kw in keywords)

def matches_none(text, excludes):
    t = text.lower()
    return not any(word_match(t, ex) for ex in excludes)

def is_controversial(v):
    """
    Controversial = has comments AND (like rate < 5% OR comment rate < 0.5%)
    like_rate    = likes / views
    comment_rate = comments / views
    """
    views    = v.get('v', 0)
    likes    = v.get('l', 0)
    comments = v.get('c', 0)
    if views == 0 or comments == 0:
        return False
    like_rate    = likes    / views   # fraction
    comment_rate = comments / views   # fraction
    return like_rate < 0.05 or comment_rate < 0.005

def top_tags(videos, n=20):
    counter = Counter()
    for v in videos:
        tags = v.get('tg', '')
        if tags and tags != '[none]':
            for t in tags.split('|'):
                t = t.strip().lower()
                if len(t) > 2:
                    counter[t] += 1
    return [{'tag': t, 'count': c} for t, c in counter.most_common(n)]

def parse_kws(raw):
    return [k.lower().strip() for k in raw.split(',') if k.strip()] if raw else []

def search(inc_any=None, inc_title=None, inc_tags=None, inc_channel=None,
           exc_any=None, exc_title=None, exc_tags=None, exc_channel=None, limit=50):
    """
    Multi-scope search. Each param is a comma-separated string or None.
    inc_* = must match that scope field
    exc_* = must NOT match that scope field
    All inc_* conditions are ANDed together (video must satisfy all).
    """
    ia  = parse_kws(inc_any)
    it  = parse_kws(inc_title)
    itg = parse_kws(inc_tags)
    ich = parse_kws(inc_channel)
    ea  = parse_kws(exc_any)
    et  = parse_kws(exc_title)
    etg = parse_kws(exc_tags)
    ech = parse_kws(exc_channel)

    has_inc = any([ia, it, itg, ich])

    matched_videos   = []
    matched_chan_ids  = set()
    chan_only_ids     = set()

    for v in VIDEOS:
        title   = v.get('t',  '').lower()
        tags    = v.get('tg', '').lower()
        desc    = v.get('de', '').lower()
        ch_name = v.get('ch', '').lower()
        all_text = ' '.join([title, tags, desc, ch_name])

        # --- EXCLUDE checks ---
        if ea  and not matches_none(all_text, ea):  continue
        if et  and not matches_none(title,    et):  continue
        if etg and not matches_none(tags,     etg): continue
        if ech and not matches_none(ch_name,  ech): continue

        # --- INCLUDE checks ---
        # Each active scoped include must be satisfied
        ok_any     = (not ia)  or matches_any(' '.join([title, tags, desc]), ia)
        ok_title   = (not it)  or matches_any(title,   it)
        ok_tags    = (not itg) or matches_any(tags,    itg)
        ok_channel = (not ich) or matches_any(ch_name, ich)

        video_match   = ok_any and ok_title and ok_tags and (ok_channel if not ich else matches_any(ch_name, ich))
        # channel-only: no video fields matched but channel name matches inc_any/inc_channel
        chan_match_only = (not video_match) and (
            (not has_inc) or
            ((not ia or matches_any(ch_name, ia)) and (not ich or matches_any(ch_name, ich)))
        ) and (not it) and (not itg)

        if video_match:
            matched_videos.append(v)
            matched_chan_ids.add(v.get('ci',''))
        elif chan_match_only and has_inc:
            chan_only_ids.add(v.get('ci',''))

    # Remove channels that also have video matches
    chan_only_ids -= matched_chan_ids

    # Popular: by views
    popular_all = sorted(matched_videos, key=lambda x: x.get('v',0), reverse=True)[:limit]

    # Controversial: must pass is_controversial filter, then sort by ratio severity
    controversial_vids = [v for v in matched_videos if is_controversial(v)]
    def controversy_sort(v):
        views = v.get('v', 1)
        like_rate    = v.get('l', 0) / views
        comment_rate = v.get('c', 0) / views
        # Lower like rate and higher comment rate = more controversial
        # Penalise by views so we surface significant videos
        score = (comment_rate / max(like_rate, 0.0001)) * (views ** 0.25)
        return score
    controversial_all = sorted(controversial_vids, key=controversy_sort, reverse=True)[:limit]

    # Per-country
    by_country = {}
    for code in COUNTRIES:
        cvids = [v for v in matched_videos if v.get('co') == code]
        if cvids:
            con_cvids = [v for v in cvids if is_controversial(v)]
            by_country[code] = {
                'popular':       sorted(cvids,     key=lambda x: x.get('v',0), reverse=True)[:limit],
                'controversial': sorted(con_cvids, key=controversy_sort,       reverse=True)[:limit],
                'total':         len(cvids),
            }

    # Channel-only list (unique)
    chan_only_list = []
    seen = set()
    for v in VIDEOS:
        cid = v.get('ci','')
        if cid in chan_only_ids and cid not in seen:
            seen.add(cid)
            chan_only_list.append({'id': cid, 'n': v.get('ch',''), 'country': v.get('co','')})

    # Top tags across matched videos
    tags = top_tags(matched_videos, n=25)

    return {
        'total_matches': len(matched_videos),
        'popular':       popular_all,
        'controversial': controversial_all,
        'by_country':    by_country,
        'channel_only':  chan_only_list[:100],
        'top_tags':      tags,
    }


def tag_stats(kw, limit=50):
    """Return what % of all videos have this keyword as a tag, plus top related tags."""
    total = len(VIDEOS)
    exact_matches = sum(1 for v in VIDEOS
        if any(word_match(t.strip(), kw) for t in v.get('tg','').split('|')))
    exact_pct = round(exact_matches / total * 100, 3) if total else 0

    # Top tags that co-occur with this keyword
    co_counter = Counter()
    for v in VIDEOS:
        tags = [t.strip().lower() for t in v.get('tg','').split('|') if t.strip() and t.strip() != '[none]']
        if any(word_match(t, kw) for t in tags):
            for t in tags:
                if t and len(t) > 2 and not word_match(t, kw):
                    co_counter[t] += 1
    top_related = [{'tag': t, 'count': c, 'pct': round(c/exact_matches*100, 1) if exact_matches else 0}
                   for t, c in co_counter.most_common(5)]
    return {'kw': kw, 'exact_pct': exact_pct, 'exact_count': exact_matches,
            'total': total, 'top_tags_for_context': top_related}

def alltime(limit=50):
    """Return pre-computed all-time cache — instant response."""
    return _ALLTIME_CACHE

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/':
            self.serve_file('search_dashboard.html', 'text/html')
        elif parsed.path == '/tag_stats':
            qs2 = parse_qs(parsed.query)
            kw = unquote(qs2.get('kw', [''])[0]).lower().strip()
            # Find exact + partial matches in global index
            exact = GLOBAL_TAG_FREQ.get(kw, 0)
            # Also count videos where kw appears in any tag
            partial = sum(c for t, c in GLOBAL_TAG_FREQ.items() if kw and kw in t)
            total = GLOBAL_TOTAL_VIDEOS
            # Top tags for context (exclude kw itself)
            top = [(t,c) for t,c in list(GLOBAL_TAG_FREQ.items())[:50] if kw not in t][:10]
            self.send_json({
                'kw': kw,
                'exact_count': exact,
                'partial_count': partial,
                'total_videos': total,
                'exact_pct': round(exact/total*100, 3) if total else 0,
                'partial_pct': round(partial/total*100, 3) if total else 0,
                'top_tags_for_context': [{'tag':t,'count':c,'pct':round(c/total*100,2)} for t,c in top],
            })
        elif parsed.path == '/tag_stats':
            kw_raw = qs.get('kw', [''])[0]
            kw = unquote(kw_raw).lower().strip() if kw_raw else ''
            self.send_json(tag_stats(kw) if kw else {})
        elif parsed.path == '/alltime':
            self.send_json(alltime())
        elif parsed.path == '/search':
            qs = parse_qs(parsed.query)
            inc_raw = qs.get('include', [''])[0]
            exc_raw = qs.get('exclude', [''])[0]
            def gp(key): 
                val = qs.get(key, [''])[0]
                return unquote(val) if val else None
            self.send_json(search(
                inc_any=gp('inc_any'), inc_title=gp('inc_title'),
                inc_tags=gp('inc_tags'), inc_channel=gp('inc_channel'),
                exc_any=gp('exc_any'), exc_title=gp('exc_title'),
                exc_tags=gp('exc_tags'), exc_channel=gp('exc_channel'),
            ))
        else:
            self.send_response(404); self.end_headers()

    def serve_file(self, filename, content_type):
        filepath = os.path.join(os.path.dirname(__file__), filename)
        try:
            with open(filepath, 'rb') as f: data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

if __name__ == '__main__':
    server = HTTPServer(('localhost', PORT), Handler)
    print(f"\n✅  http://localhost:{PORT}  —  Ctrl+C to stop\n")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
