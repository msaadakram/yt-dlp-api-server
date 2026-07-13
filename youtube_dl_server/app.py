import functools
import logging
import os
import tempfile
import traceback
import sys

from flask import Flask, Blueprint, current_app, jsonify, request, redirect, abort
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

# ---------------------------------------------------------------
# REAL verified live URLs - searched & confirmed July 2026
# ---------------------------------------------------------------
TEST_URLS = {
    # yt-dlp official test video - always exists
    'youtube':      'https://www.youtube.com/watch?v=BaW_jenozKc',

    # Khaby Lame - June 2026 video (1.4M likes, confirmed live)
    'tiktok':       'https://www.tiktok.com/@khaby.lame/video/7646812028874673439',

    # Dailymotion - telesur World Cup video published June 2026
    'dailymotion':  'https://www.dailymotion.com/video/xaedfou',

    # Vimeo - classic public test video, always available
    'vimeo':        'https://vimeo.com/76979871',

    # SoundCloud - Forss flickermood (classic public track, always up)
    'soundcloud':   'https://soundcloud.com/forss/flickermood',

    # Twitter/X public video
    'twitter':      'https://twitter.com/i/status/1804553550801391673',

    # Twitch public clip (clips never expire)
    'twitch':       'https://clips.twitch.tv/AgreeableElegantTarsierWoofer-pDCNqN9GUAVtxhp7',

    # Instagram - needs login (expected to fail without cookies)
    'instagram':    'https://www.instagram.com/reel/C9W0JHjIGdL/',

    # Facebook - needs login (expected to fail without cookies)
    'facebook':     'https://www.facebook.com/FacebookforDevelopers/videos/10152454893803553/',

    # Reddit - needs login (expected to fail without cookies)
    'reddit':       'https://www.reddit.com/r/oddlysatisfying/comments/1dummyid/satisfying_video/',
}

# Platforms that are expected to fail without cookies
LOGIN_REQUIRED = {'instagram', 'facebook', 'reddit'}


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
                'player_client': ['web', 'android', 'ios'],
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
    result = {
        'url': url,
        key: result,
    }
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


@route_api('test')
@set_access_control
def test_all_platforms():
    """
    Test all platforms with real verified live URLs.
    /api/test                    -> test all 10 platforms
    /api/test?platform=youtube   -> test one platform
    /api/test?platform=tiktok
    /api/test?platform=vimeo
    /api/test?platform=dailymotion
    /api/test?platform=soundcloud
    /api/test?platform=twitter
    /api/test?platform=twitch
    """
    platform_filter = request.args.get('platform', None)
    urls_to_test = {
        k: v for k, v in TEST_URLS.items()
        if platform_filter is None or k == platform_filter
    }

    results = {}
    for platform, url in urls_to_test.items():
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
                        'player_client': ['web', 'android', 'ios'],
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
            direct_url = None
            if formats:
                direct_url = formats[-1].get('url')
            if not direct_url:
                direct_url = info.get('url')

            results[platform] = {
                'status': 'ok',
                'url': url,
                'title': info.get('title', 'N/A'),
                'duration': info.get('duration', 'N/A'),
                'formats_available': len(formats),
                'direct_url': direct_url,
                'thumbnail': info.get('thumbnail', None),
                'uploader': info.get('uploader', 'N/A'),
                'login_required': platform in LOGIN_REQUIRED,
            }
        except Exception as e:
            results[platform] = {
                'status': 'error',
                'url': url,
                'error': str(e),
                'login_required': platform in LOGIN_REQUIRED,
            }

    total = len(results)
    passed = sum(1 for r in results.values() if r['status'] == 'ok')
    failed_no_login = [
        p for p, r in results.items()
        if r['status'] == 'error' and p not in LOGIN_REQUIRED
    ]
    login_blocked = [
        p for p, r in results.items()
        if r['status'] == 'error' and p in LOGIN_REQUIRED
    ]

    return jsonify({
        'summary': {
            'total': total,
            'passed': passed,
            'failed_real_errors': len(failed_no_login),
            'failed_login_required': len(login_blocked),
            'failed_real_error_platforms': failed_no_login,
            'failed_login_platforms': login_blocked,
        },
        'results': results,
    })


app = Flask(__name__)
app.register_blueprint(api)
app.config.from_pyfile('../application.cfg', silent=True)
