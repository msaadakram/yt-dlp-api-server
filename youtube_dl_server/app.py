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


_COOKIES_ENV = os.environ.get('YOUTUBE_COOKIES', '')
_COOKIES_REPO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cookies.txt')
_COOKIES_TEMP_PATH = None

def _get_cookies_file():
    global _COOKIES_TEMP_PATH
    if _COOKIES_ENV:
        if _COOKIES_TEMP_PATH is None or not os.path.isfile(_COOKIES_TEMP_PATH):
            tmp = tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', prefix='yt_cookies_', delete=False
            )
            tmp.write(_COOKIES_ENV)
            tmp.flush()
            tmp.close()
            _COOKIES_TEMP_PATH = tmp.name
        return _COOKIES_TEMP_PATH
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

TEST_URLS = {
    'youtube':      'https://www.youtube.com/watch?v=BaW_jenozKc',
    'tiktok':       'https://www.tiktok.com/@khaby.lame/video/7646812028874673439',
    'dailymotion':  'https://www.dailymotion.com/video/xaedfou',
    'vimeo':        'https://vimeo.com/76979871',
    'soundcloud':   'https://soundcloud.com/forss/flickermood',
    'twitter':      'https://x.com/i/status/1876345576239841773',
    'twitch':       'https://clips.twitch.tv/AttractiveObliviousFerretTheTarFu-gbLQE2LoKjjzgEMk',
    'instagram':    'https://www.instagram.com/reel/C9W0JHjIGdL/',
    'facebook':     'https://www.facebook.com/FacebookforDevelopers/videos/10152454893803553/',
    'reddit':       'https://www.reddit.com/r/oddlysatisfying/comments/1dummyid/satisfying_video/',
}

LOGIN_REQUIRED = {'instagram', 'facebook', 'reddit'}

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
                'player_client': ['web', 'tv_embedded', 'android', 'ios'],
                'skip': ['translated_subs'],
            }
        },
    }
    cookies_path = _get_cookies_file()
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
        message = '"{}" expects a {}, got "{}"'.format(parameter, type, value)
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
                    'player_client': ['web', 'tv_embedded', 'android', 'ios'],
                    'skip': ['translated_subs'],
                }
            },
        }
        cookies_path = _get_cookies_file()
        if cookies_path:
            ydl_params['cookiefile'] = cookies_path

        with yt_dlp.YoutubeDL(ydl_params) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get('formats', [])
        direct_url = (formats[-1].get('url') if formats else None) or info.get('url')

        # Build formats list: ext, resolution, filesize
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
            'status':           'ok',
            'url':              url,
            'title':            info.get('title', 'N/A'),
            'uploader':         info.get('uploader', 'N/A'),
            'duration':         info.get('duration', 'N/A'),
            'view_count':       info.get('view_count'),
            'thumbnail':        info.get('thumbnail'),
            'formats_available': len(formats),
            'formats':          fmt_list,
            'direct_url':       direct_url,
            'login_required':   platform in LOGIN_REQUIRED,
            'elapsed_sec':      elapsed,
        }
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        return {
            'status':         'error',
            'url':            url,
            'error':          str(e),
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

    cards = ''
    for platform, r in sorted(results.items()):
        icon     = PLATFORM_ICONS.get(platform, '\U0001f310')
        is_ok    = r['status'] == 'ok'
        is_login = r.get('login_required', False)
        status_badge = (
            '<span class="badge ok">\u2705 OK</span>' if is_ok else
            '<span class="badge login">\U0001f512 Login Required</span>' if is_login else
            '<span class="badge err">\u274c ERROR</span>'
        )
        thumb_html = ''
        if is_ok and r.get('thumbnail'):
            thumb_html = '<img src="{}" class="thumb" alt="thumbnail">'.format(r['thumbnail'])

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
                meta_rows += '<tr><td>Direct URL</td><td class="val url-cell"><a href="{}" target="_blank">Open stream &#8599;</a></td></tr>'.format(r['direct_url'])

            # formats table
            fmts = r.get('formats', [])
            fmt_table = ''
            if fmts:
                fmt_table = '''
            <details>
              <summary>Show all {} formats</summary>
              <table class="fmt-table">
                <thead><tr><th>ID</th><th>Ext</th><th>Resolution</th><th>VCodec</th><th>ACodec</th><th>Bitrate</th><th>Size</th></tr></thead>
                <tbody>
            '''.format(len(fmts))
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
            meta_rows += '<tr><td>Elapsed</td><td class="val">{} s</td></tr>'.format(r.get('elapsed_sec', 'N/A'))
            fmt_table = ''

        cards += '''
    <div class="card {cls}">
      <div class="card-header">
        <span class="platform-icon">{icon}</span>
        <span class="platform-name">{name}</span>
        {badge}
      </div>
      {thumb}
      <table class="meta-table"><tbody>{meta}</tbody></table>
      {fmt_table}
      <div class="source-url"><a href="{url}" target="_blank">{url}</a></div>
    </div>
    '''.format(
            cls='card-ok' if is_ok else ('card-login' if is_login else 'card-err'),
            icon=icon,
            name=platform.upper(),
            badge=status_badge,
            thumb=thumb_html,
            meta=meta_rows,
            fmt_table=fmt_table,
            url=r['url'],
        )

    platform_buttons = ''.join(
        '<a href="/api/test?platform={p}" class="btn-plat">{i} {p}</a>'.format(
            p=p, i=PLATFORM_ICONS.get(p,''))
        for p in sorted(TEST_URLS)
    )

    html = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt-dlp API — Platform Test Dashboard</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:24px}}
  h1{{font-size:1.8rem;font-weight:700;color:#f8fafc;margin-bottom:4px}}
  .subtitle{{color:#94a3b8;font-size:.9rem;margin-bottom:20px}}
  .score-bar{{display:flex;align-items:center;gap:16px;background:#1e293b;border-radius:12px;padding:16px 24px;margin-bottom:24px}}
  .score-num{{font-size:2.4rem;font-weight:800;color:{score_color}}}
  .score-label{{color:#94a3b8;font-size:.9rem}}
  .score-detail{{margin-left:auto;font-size:.85rem;color:#64748b}}
  .btn-plat{{display:inline-block;padding:6px 14px;margin:4px;border-radius:8px;background:#1e293b;color:#94a3b8;text-decoration:none;font-size:.82rem;border:1px solid #334155;transition:.15s}}
  .btn-plat:hover{{background:#334155;color:#f1f5f9}}
  .btn-all{{background:#3b82f6;color:#fff;border-color:#3b82f6}}
  .btn-all:hover{{background:#2563eb}}
  .filter-bar{{margin-bottom:24px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px}}
  .card{{background:#1e293b;border-radius:14px;overflow:hidden;border:1px solid #334155}}
  .card-ok{{border-color:#166534}}
  .card-err{{border-color:#7f1d1d}}
  .card-login{{border-color:#78350f}}
  .card-header{{display:flex;align-items:center;gap:10px;padding:14px 16px;background:#0f172a;border-bottom:1px solid #334155}}
  .platform-icon{{font-size:1.4rem}}
  .platform-name{{font-weight:700;font-size:1rem;letter-spacing:.05em;flex:1}}
  .badge{{font-size:.75rem;padding:3px 10px;border-radius:20px;font-weight:600}}
  .badge.ok{{background:#14532d;color:#86efac}}
  .badge.err{{background:#7f1d1d;color:#fca5a5}}
  .badge.login{{background:#78350f;color:#fcd34d}}
  .thumb{{width:100%;height:180px;object-fit:cover;display:block}}
  .meta-table{{width:100%;border-collapse:collapse;font-size:.83rem}}
  .meta-table td{{padding:7px 14px;border-bottom:1px solid #1e293b}}
  .meta-table td:first-child{{color:#64748b;width:90px;white-space:nowrap}}
  .val{{color:#e2e8f0;word-break:break-all}}
  .url-cell a{{color:#60a5fa;text-decoration:none}}
  .url-cell a:hover{{text-decoration:underline}}
  .err-msg{{color:#fca5a5;font-size:.78rem}}
  .source-url{{padding:8px 14px;font-size:.72rem;color:#475569;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .source-url a{{color:#475569;text-decoration:none}}
  .source-url a:hover{{color:#94a3b8}}
  details summary{{padding:8px 14px;font-size:.8rem;color:#60a5fa;cursor:pointer;list-style:none}}
  details summary::-webkit-details-marker{{display:none}}
  .fmt-table{{width:100%;border-collapse:collapse;font-size:.75rem;margin:0}}
  .fmt-table th{{background:#0f172a;color:#64748b;padding:5px 10px;text-align:left;font-weight:600}}
  .fmt-table td{{padding:4px 10px;border-top:1px solid #0f172a;color:#cbd5e1}}
  .fmt-table tr:hover td{{background:#1e293b}}
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
  <div class="score-detail">
    &#10003; {passed} OK &nbsp;&nbsp;
    &#10007; {failed_real} real errors &nbsp;&nbsp;
    &#128274; {failed_login} login required
  </div>
</div>

<div class="filter-bar">
  <a href="/api/test" class="btn-plat btn-all">&#9654; All Platforms</a>
  {platform_buttons}
</div>

<div class="grid">
  {cards}
</div>
</body>
</html>'''.format(
        score_color=score_color,
        ytdlp_ver=yt_dlp_version,
        api_ver=__version__,
        total=total,
        passed=passed,
        failed_real=summary['failed_real_errors'],
        failed_login=summary['failed_login_required'],
        platform_buttons=platform_buttons,
        cards=cards,
    )
    return html


@route_api('test')
@set_access_control
def test_all_platforms():
    """
    Beautiful HTML dashboard showing full test results for all platforms.
    /api/test                    -> test all platforms
    /api/test?platform=youtube   -> test one platform
    /api/test?format=json        -> raw JSON response
    """
    platform_filter  = request.args.get('platform', None)
    response_format  = request.args.get('format', 'html')

    urls_to_test = {
        k: v for k, v in TEST_URLS.items()
        if platform_filter is None or k == platform_filter
    }

    results = {}
    for platform, url in urls_to_test.items():
        results[platform] = _run_test(platform, url)

    total       = len(results)
    passed      = sum(1 for r in results.values() if r['status'] == 'ok')
    failed_no_login = [p for p, r in results.items() if r['status'] == 'error' and p not in LOGIN_REQUIRED]
    login_blocked   = [p for p, r in results.items() if r['status'] == 'error' and p in LOGIN_REQUIRED]

    summary = {
        'total':                    total,
        'passed':                   passed,
        'failed_real_errors':       len(failed_no_login),
        'failed_login_required':    len(login_blocked),
        'failed_real_error_platforms': failed_no_login,
        'failed_login_platforms':   login_blocked,
    }

    if response_format == 'json':
        return jsonify({'summary': summary, 'results': results})

    html = _build_html(results, summary, platform_filter)
    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp


app = Flask(__name__)
app.register_blueprint(api)
app.config.from_pyfile('../application.cfg', silent=True)
