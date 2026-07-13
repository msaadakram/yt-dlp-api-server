import functools
import logging
import os
import tempfile
import traceback
import sys
import time

from flask import Flask, Blueprint, current_app, jsonify, request, redirect, abort, make_response
import yt_dlp
from yt_dlp.version import __version__ as yt_dlp_version

from .version import __version__


if not hasattr(sys.stderr, 'isatty'):
    sys.stderr.isatty = lambda: False


# ---------------------------------------------------------------
# Per-platform cookie resolution
# ---------------------------------------------------------------
_COOKIES_REPO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cookies.txt')

PLATFORM_COOKIE_ENV = {
    'instagram':   'INSTAGRAM_COOKIES',
    'facebook':    'FACEBOOK_COOKIES',
    'reddit':      'REDDIT_COOKIES',
    'twitter':     'TWITTER_COOKIES',
    'x.com':       'TWITTER_COOKIES',
    'tiktok':      'TIKTOK_COOKIES',
    'youtube':     'YOUTUBE_COOKIES',
    'youtu.be':    'YOUTUBE_COOKIES',
}

_COOKIE_TEMP_CACHE = {}


def _write_temp_cookies(env_var_name):
    if env_var_name in _COOKIE_TEMP_CACHE:
        path = _COOKIE_TEMP_CACHE[env_var_name]
        if os.path.isfile(path):
            return path
    content = os.environ.get(env_var_name, '')
    if not content:
        return None
    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.txt', prefix='cookies_{}_'.format(env_var_name), delete=False
    )
    tmp.write(content)
    tmp.flush()
    tmp.close()
    _COOKIE_TEMP_CACHE[env_var_name] = tmp.name
    return tmp.name


def _get_cookies_for_url(url):
    url_lower = url.lower()
    for keyword, env_var in PLATFORM_COOKIE_ENV.items():
        if keyword in url_lower:
            path = _write_temp_cookies(env_var)
            if path:
                return path
            break
    generic = _write_temp_cookies('YOUTUBE_COOKIES')
    if generic:
        return generic
    if os.path.isfile(_COOKIES_REPO_FILE):
        return _COOKIES_REPO_FILE
    return None


DEFAULT_FORMAT = (
    'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]'
    '/bestvideo[ext=mp4]+bestaudio[ext=m4a]'
    '/bestvideo+bestaudio'
    '/mp4'
    '/best'
    '/bestvideo'
    '/bestaudio'
)

# ---------------------------------------------------------------
# TEST URLS — verified live July 2026
# ---------------------------------------------------------------
TEST_URLS = {
    # yt-dlp official test video
    'youtube':      'https://www.youtube.com/watch?v=BaW_jenozKc',

    # Khaby Lame — 15.7M views, confirmed live
    'tiktok':       'https://www.tiktok.com/@khaby.lame/video/7646812028874673439',

    # Dailymotion — recent news clip, confirmed live
    'dailymotion':  'https://www.dailymotion.com/video/xaedfou',

    # Vimeo — classic public test video
    'vimeo':        'https://vimeo.com/76979871',

    # SoundCloud — always public
    'soundcloud':   'https://soundcloud.com/forss/flickermood',

    # Twitter/X — needs login cookies for video extraction
    'twitter':      'https://x.com/i/status/1876345576239841773',

    # Twitch — confirmed live clip
    'twitch':       'https://clips.twitch.tv/AttractiveObliviousFerretTheTarFu-gbLQE2LoKjjzgEMk',

    # Instagram — NASA public reel (large verified account, reliable)
    'instagram':    'https://www.instagram.com/reel/C8p1oWXuF3N/',

    # Facebook — NASA public video (public page, no login wall)
    'facebook':     'https://www.facebook.com/NASA/videos/1539781023275888/',

    # Reddit — real video post on r/nextfuckinglevel (verified exists)
    'reddit':       'https://www.reddit.com/r/nextfuckinglevel/comments/1cqxrdl/this_soccer_player_is_absolutely_insane/',
}

# Platforms that need login cookies — twitter also needs them for video
LOGIN_REQUIRED = {'instagram', 'facebook', 'reddit', 'twitter'}

PLATFORM_ICONS = {
    'youtube':     '\U0001f534',
    'tiktok':      '\U0001f3b5',
    'dailymotion': '\U0001f4fa',
    'vimeo':       '\U0001f3ac',
    'soundcloud':  '\U0001f3a7',
    'twitter':     '\U0001f426',
    'twitch':      '\U0001f7e3',
    'instagram':   '\U0001f4f8',
    'facebook':    '\U0001f4d8',
    'reddit':      '\U0001f9e1',
}


class SimpleYDL(yt_dlp.YoutubeDL):
    def __init__(self, *args, **kargs):
        super(SimpleYDL, self).__init__(*args, **kargs)
        self.add_default_info_extractors()


def get_videos(url, extra_params):
    ydl_params = {
        'format': DEFAULT_FORMAT,
        'cachedir': False,
        'logger': current_app.logger.getChild('youtube-dl'),
        'ignore_no_formats_error': True,
        'extractor_retries': 3,
        'skip_unavailable_fragments': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['web', 'mweb', 'tv_embedded', 'android', 'ios'],
                'skip': ['translated_subs'],
            }
        },
    }
    cookies_path = _get_cookies_for_url(url)
    if cookies_path:
        ydl_params['cookiefile'] = cookies_path
    ydl_params.update(extra_params)
    ydl = SimpleYDL(ydl_params)
    res = ydl.extract_info(url, download=False)
    return res


def flatten_result(result):
    r_type = result.get('_type', 'video')
    if r_type == 'video':
        videos = [result]
    elif r_type == 'playlist':
        videos = []
        for entry in result['entries']:
            videos.extend(flatten_result(entry))
    elif r_type == 'compat_list':
        videos = []
        for r in result['entries']:
            videos.extend(flatten_result(r))
    return videos


api = Blueprint('api', __name__)


def route_api(subpath, *args, **kargs):
    return api.route('/api/' + subpath, *args, **kargs)


def set_access_control(f):
    @functools.wraps(f)
    def wrapper(*args, **kargs):
        response = f(*args, **kargs)
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    return wrapper


@api.errorhandler(yt_dlp.utils.DownloadError)
@api.errorhandler(yt_dlp.utils.ExtractorError)
def handle_youtube_dl_error(error):
    logging.error(traceback.format_exc())
    result = jsonify({'error': str(error)})
    result.status_code = 500
    return result


class WrongParameterTypeError(ValueError):
    def __init__(self, value, type, parameter):
        message = '"{}\" expects a {}, got \"{}\"'.format(parameter, type, value)
        super(WrongParameterTypeError, self).__init__(message)


@api.errorhandler(WrongParameterTypeError)
def handle_wrong_parameter(error):
    logging.error(traceback.format_exc())
    result = jsonify({'error': str(error)})
    result.status_code = 400
    return result


@api.before_request
def block_on_user_agent():
    user_agent = request.user_agent.string
    forbidden_uas = current_app.config.get('FORBIDDEN_USER_AGENTS', [])
    if user_agent in forbidden_uas:
        abort(429)


def query_bool(value, name, default=None):
    if value is None:
        return default
    value = value.lower()
    if value == 'true':
        return True
    elif value == 'false':
        return False
    else:
        raise WrongParameterTypeError(value, 'bool', name)


ALLOWED_EXTRA_PARAMS = {
    'format': str,
    'playliststart': int,
    'playlistend': int,
    'playlist_items': str,
    'playlistreverse': bool,
    'matchtitle': str,
    'rejecttitle': str,
    'writesubtitles': bool,
    'writeautomaticsub': bool,
    'allsubtitles': bool,
    'subtitlesformat': str,
    'subtitleslangs': list,
}


def get_result():
    url = request.args['url']
    extra_params = {}
    for k, v in request.args.items():
        if k in ALLOWED_EXTRA_PARAMS:
            convertf = ALLOWED_EXTRA_PARAMS[k]
            if convertf == bool:
                convertf = lambda x: query_bool(x, k)
            elif convertf == list:
                convertf = lambda x: x.split(',')
            extra_params[k] = convertf(v)
    return get_videos(url, extra_params)


@route_api('info')
@set_access_control
def info():
    url = request.args['url']
    result = get_result()
    key = 'info'
    if query_bool(request.args.get('flatten'), 'flatten', False):
        result = flatten_result(result)
        key = 'videos'
    result = {'url': url, key: result}
    return jsonify(result)


@route_api('play')
def play():
    result = flatten_result(get_result())
    return redirect(result[0]['url'])


@route_api('extractors')
@set_access_control
def list_extractors():
    ie_list = [{
        'name': ie.IE_NAME,
        'working': ie.working(),
    } for ie in yt_dlp.gen_extractors()]
    return jsonify(extractors=ie_list)


@route_api('version')
@set_access_control
def version():
    result = {
        'yt-dlp': yt_dlp_version,
        'yt-dlp-api-server': __version__,
    }
    return jsonify(result)


@route_api('cookies/status')
@set_access_control
def cookies_status():
    all_vars = [
        'YOUTUBE_COOKIES', 'INSTAGRAM_COOKIES', 'FACEBOOK_COOKIES',
        'REDDIT_COOKIES', 'TWITTER_COOKIES', 'TIKTOK_COOKIES',
    ]
    status = {}
    for var in all_vars:
        val = os.environ.get(var, '')
        status[var] = {
            'configured': bool(val),
            'length': len(val) if val else 0,
        }
    return jsonify({'cookies': status})


def _run_test(platform, url):
    t0 = time.time()
    try:
        ydl_params = {
            'format': DEFAULT_FORMAT,
            'cachedir': False,
            'quiet': True,
            'no_warnings': True,
            'ignore_no_formats_error': True,
            'extractor_retries': 2,
            'skip_unavailable_fragments': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['web', 'mweb', 'tv_embedded', 'android', 'ios'],
                    'skip': ['translated_subs'],
                }
            },
        }
        cookies_path = _get_cookies_for_url(url)
        if cookies_path:
            ydl_params['cookiefile'] = cookies_path

        with yt_dlp.YoutubeDL(ydl_params) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get('formats', [])
        direct_url = (formats[-1].get('url') if formats else None) or info.get('url')

        fmt_list = []
        for f in formats:
            fmt_list.append({
                'id':         f.get('format_id', ''),
                'ext':        f.get('ext', ''),
                'resolution': f.get('resolution') or '{}x{}'.format(f.get('width','?'), f.get('height','?')),
                'vcodec':     f.get('vcodec', 'none'),
                'acodec':     f.get('acodec', 'none'),
                'filesize':   f.get('filesize') or f.get('filesize_approx'),
                'tbr':        f.get('tbr'),
                'url':        f.get('url', ''),
            })

        elapsed = round(time.time() - t0, 2)
        return {
            'status':            'ok',
            'url':               url,
            'title':             info.get('title', 'N/A'),
            'uploader':          info.get('uploader', 'N/A'),
            'duration':          info.get('duration', 'N/A'),
            'view_count':        info.get('view_count'),
            'thumbnail':         info.get('thumbnail'),
            'formats_available': len(formats),
            'formats':           fmt_list,
            'direct_url':        direct_url,
            'cookies_used':      bool(cookies_path),
            'login_required':    platform in LOGIN_REQUIRED,
            'elapsed_sec':       elapsed,
        }
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        return {
            'status':         'error',
            'url':            url,
            'error':          str(e),
            'cookies_used':   bool(_get_cookies_for_url(url)),
            'login_required': platform in LOGIN_REQUIRED,
            'elapsed_sec':    elapsed,
        }


def _fmt_bytes(b):
    if not b:
        return 'N/A'
    for unit in ['B','KB','MB','GB']:
        if b < 1024:
            return '{:.1f} {}'.format(b, unit)
        b /= 1024
    return '{:.1f} TB'.format(b)


def _fmt_dur(s):
    if not s or s == 'N/A':
        return 'N/A'
    try:
        s = int(s)
        return '{}:{:02d}'.format(s // 60, s % 60)
    except Exception:
        return str(s)


def _build_html(results, summary, platform_filter):
    passed = summary['passed']
    total  = summary['total']
    score_color = '#22c55e' if passed == total else ('#f59e0b' if passed >= total // 2 else '#ef4444')

    cookie_vars = ['YOUTUBE_COOKIES','INSTAGRAM_COOKIES','FACEBOOK_COOKIES','REDDIT_COOKIES','TWITTER_COOKIES','TIKTOK_COOKIES']
    cookie_pills = ''
    for var in cookie_vars:
        configured = bool(os.environ.get(var, ''))
        label = var.replace('_COOKIES','')
        cookie_pills += '<span class="cpill {}\">{} {}</span>'.format(
            'cpill-ok' if configured else 'cpill-no',
            '\u2714' if configured else '\u2717',
            label
        )

    cards = ''
    for platform, r in sorted(results.items()):
        icon     = PLATFORM_ICONS.get(platform, '\U0001f310')
        is_ok    = r['status'] == 'ok'
        is_login = r.get('login_required', False)
        cookies_used = r.get('cookies_used', False)
        status_badge = (
            '<span class="badge ok">&#10003; OK</span>' if is_ok else
            '<span class="badge login">&#128274; Login Required</span>' if (is_login and not cookies_used) else
            '<span class="badge err">&#10007; ERROR</span>'
        )
        cookie_badge = '<span class="badge cookie">&#127850; cookies</span>' if cookies_used else ''

        thumb_html = ''
        if is_ok and r.get('thumbnail'):
            thumb_html = '<img src=\"{}\" class="thumb" alt="thumbnail">'.format(r['thumbnail'])

        meta_rows = ''
        if is_ok:
            meta_rows += '<tr><td>Title</td><td class="val">{}</td></tr>'.format(r.get('title','N/A'))
            meta_rows += '<tr><td>Uploader</td><td class="val">{}</td></tr>'.format(r.get('uploader','N/A'))
            meta_rows += '<tr><td>Duration</td><td class="val">{}</td></tr>'.format(_fmt_dur(r.get('duration')))
            if r.get('view_count'):
                meta_rows += '<tr><td>Views</td><td class="val">{:,}</td></tr>'.format(r['view_count'])
            meta_rows += '<tr><td>Formats</td><td class="val">{}</td></tr>'.format(r.get('formats_available',0))
            meta_rows += '<tr><td>Elapsed</td><td class="val">{} s</td></tr>'.format(r.get('elapsed_sec'))
            if r.get('direct_url'):
                meta_rows += '<tr><td>Direct URL</td><td class="val url-cell"><a href=\"{}\" target="_blank">Open stream &#8599;</a></td></tr>'.format(r['direct_url'])

            fmts = r.get('formats', [])
            fmt_table = ''
            if fmts:
                fmt_table = '<details><summary>Show all {} formats</summary><table class="fmt-table"><thead><tr><th>ID</th><th>Ext</th><th>Resolution</th><th>VCodec</th><th>ACodec</th><th>Bitrate</th><th>Size</th></tr></thead><tbody>'.format(len(fmts))
                for f in fmts:
                    fmt_table += '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
                        f.get('id',''), f.get('ext',''), f.get('resolution',''),
                        f.get('vcodec',''), f.get('acodec',''),
                        '{} kbps'.format(round(f['tbr'])) if f.get('tbr') else 'N/A',
                        _fmt_bytes(f.get('filesize'))
                    )
                fmt_table += '</tbody></table></details>'
        else:
            err_msg = r.get('error', 'Unknown error')
            meta_rows += '<tr><td>Error</td><td class="val err-msg">{}</td></tr>'.format(err_msg)
            meta_rows += '<tr><td>Elapsed</td><td class="val">{} s</td></tr>'.format(r.get('elapsed_sec','N/A'))
            fmt_table = ''

        cards += '''
    <div class="card {cls}">
      <div class="card-header">
        <span class="platform-icon">{icon}</span>
        <span class="platform-name">{name}</span>
        {badge}{cookie_badge}
      </div>
      {thumb}
      <table class="meta-table"><tbody>{meta}</tbody></table>
      {fmt_table}
      <div class="source-url"><a href="{url}" target="_blank">{url}</a></div>
    </div>
    '''.format(
            cls='card-ok' if is_ok else ('card-login' if (is_login and not cookies_used) else 'card-err'),
            icon=icon, name=platform.upper(),
            badge=status_badge, cookie_badge=cookie_badge,
            thumb=thumb_html, meta=meta_rows,
            fmt_table=fmt_table, url=r['url'],
        )

    platform_buttons = ''.join(
        '<a href="/api/test?platform={p}&format=html" class="btn-plat">{i} {p}</a>'.format(
            p=p, i=PLATFORM_ICONS.get(p,''))
        for p in sorted(TEST_URLS)
    )

    html = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt-dlp API \u2014 Platform Test Dashboard</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:24px}}
  h1{{font-size:1.8rem;font-weight:700;color:#f8fafc;margin-bottom:4px}}
  .subtitle{{color:#94a3b8;font-size:.9rem;margin-bottom:16px}}
  .score-bar{{display:flex;align-items:center;gap:16px;background:#1e293b;border-radius:12px;padding:16px 24px;margin-bottom:16px;flex-wrap:wrap}}
  .score-num{{font-size:2.4rem;font-weight:800;color:{score_color}}}
  .score-label{{color:#94a3b8;font-size:.9rem}}
  .score-detail{{margin-left:auto;font-size:.85rem;color:#64748b}}
  .cookie-bar{{display:flex;flex-wrap:wrap;gap:8px;background:#1e293b;border-radius:10px;padding:12px 20px;margin-bottom:20px;align-items:center}}
  .cookie-bar-label{{font-size:.8rem;color:#64748b;margin-right:4px}}
  .cpill{{font-size:.75rem;padding:3px 10px;border-radius:20px;font-weight:600}}
  .cpill-ok{{background:#14532d;color:#86efac}}
  .cpill-no{{background:#1e293b;color:#475569;border:1px solid #334155}}
  .btn-plat{{display:inline-block;padding:6px 14px;margin:4px;border-radius:8px;background:#1e293b;color:#94a3b8;text-decoration:none;font-size:.82rem;border:1px solid #334155}}
  .btn-plat:hover{{background:#334155;color:#f1f5f9}}
  .btn-all{{background:#3b82f6;color:#fff;border-color:#3b82f6}}
  .btn-json{{background:#7c3aed;color:#fff;border-color:#7c3aed}}
  .filter-bar{{margin-bottom:24px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px}}
  .card{{background:#1e293b;border-radius:14px;overflow:hidden;border:1px solid #334155}}
  .card-ok{{border-color:#166534}}
  .card-err{{border-color:#7f1d1d}}
  .card-login{{border-color:#78350f}}
  .card-header{{display:flex;align-items:center;gap:8px;padding:14px 16px;background:#0f172a;border-bottom:1px solid #334155;flex-wrap:wrap}}
  .platform-icon{{font-size:1.4rem}}
  .platform-name{{font-weight:700;font-size:1rem;letter-spacing:.05em;flex:1}}
  .badge{{font-size:.72rem;padding:3px 9px;border-radius:20px;font-weight:600}}
  .badge.ok{{background:#14532d;color:#86efac}}
  .badge.err{{background:#7f1d1d;color:#fca5a5}}
  .badge.login{{background:#78350f;color:#fcd34d}}
  .badge.cookie{{background:#1e3a5f;color:#93c5fd}}
  .thumb{{width:100%;height:180px;object-fit:cover;display:block}}
  .meta-table{{width:100%;border-collapse:collapse;font-size:.83rem}}
  .meta-table td{{padding:7px 14px;border-bottom:1px solid #0f172a}}
  .meta-table td:first-child{{color:#64748b;width:90px;white-space:nowrap}}
  .val{{color:#e2e8f0;word-break:break-all}}
  .url-cell a{{color:#60a5fa;text-decoration:none}}
  .url-cell a:hover{{text-decoration:underline}}
  .err-msg{{color:#fca5a5;font-size:.78rem}}
  .source-url{{padding:8px 14px;font-size:.72rem;color:#475569;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .source-url a{{color:#475569;text-decoration:none}}
  details summary{{padding:8px 14px;font-size:.8rem;color:#60a5fa;cursor:pointer;list-style:none}}
  details summary::-webkit-details-marker{{display:none}}
  .fmt-table{{width:100%;border-collapse:collapse;font-size:.75rem}}
  .fmt-table th{{background:#0f172a;color:#64748b;padding:5px 10px;text-align:left;font-weight:600}}
  .fmt-table td{{padding:4px 10px;border-top:1px solid #0f172a;color:#cbd5e1}}
  @media(max-width:600px){{.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<h1>&#127916; yt-dlp API &mdash; Platform Test Dashboard</h1>
<p class="subtitle">yt-dlp v{ytdlp_ver} &nbsp;&bull;&nbsp; API Server v{api_ver} &nbsp;&bull;&nbsp; Tested {total} platforms</p>

<div class="score-bar">
  <div>
    <div class="score-num" style="color:{score_color}">{passed}/{total}</div>
    <div class="score-label">platforms passing</div>
  </div>
  <div class="score-detail">&#10003; {passed} OK &nbsp;&nbsp; &#10007; {failed_real} real errors &nbsp;&nbsp; &#128274; {failed_login} login blocked</div>
</div>

<div class="cookie-bar">
  <span class="cookie-bar-label">&#127850; Cookies:</span>
  {cookie_pills}
  <a href="/api/cookies/status" style="margin-left:auto;font-size:.75rem;color:#475569;text-decoration:none">full status &#8599;</a>
</div>

<div class="filter-bar">
  <a href="/api/test?format=html" class="btn-plat btn-all">&#9654; All Platforms</a>
  {platform_buttons}
  <a href="/api/test" class="btn-plat btn-json" style="margin-left:8px">&#123;&#125; JSON</a>
</div>

<div class="grid">{cards}</div>
</body></html>'''.format(
        score_color=score_color,
        ytdlp_ver=yt_dlp_version,
        api_ver=__version__,
        total=total, passed=passed,
        failed_real=summary['failed_real_errors'],
        failed_login=summary['failed_login_required'],
        cookie_pills=cookie_pills,
        platform_buttons=platform_buttons,
        cards=cards,
    )
    return html


@route_api('test')
@set_access_control
def test_all_platforms():
    """
    /api/test                      -> JSON (default)
    /api/test?format=html          -> HTML dashboard
    /api/test?platform=youtube     -> single platform JSON
    /api/test?platform=youtube&format=html -> single platform HTML
    """
    platform_filter = request.args.get('platform', None)
    response_format = request.args.get('format', 'json')

    urls_to_test = {
        k: v for k, v in TEST_URLS.items()
        if platform_filter is None or k == platform_filter
    }

    results = {p: _run_test(p, u) for p, u in urls_to_test.items()}

    total           = len(results)
    passed          = sum(1 for r in results.values() if r['status'] == 'ok')
    failed_no_login = [p for p, r in results.items() if r['status'] == 'error' and p not in LOGIN_REQUIRED]
    login_blocked   = [p for p, r in results.items() if r['status'] == 'error' and p in LOGIN_REQUIRED]

    summary = {
        'total': total, 'passed': passed,
        'failed_real_errors': len(failed_no_login),
        'failed_login_required': len(login_blocked),
        'failed_real_error_platforms': failed_no_login,
        'failed_login_platforms': login_blocked,
    }

    if response_format == 'html':
        html = _build_html(results, summary, platform_filter)
        resp = make_response(html)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        return resp

    return jsonify({'summary': summary, 'results': results})


app = Flask(__name__)
app.register_blueprint(api)
app.config.from_pyfile('../application.cfg', silent=True)
