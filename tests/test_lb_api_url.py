"""ListenBrainz calls made with the user's token go to the scrobbler's server: listenbrainz-mpd's [submission] api_url
(trailing slashes dropped, /1/... appended, as listenbrainz-mpd does), else the public ListenBrainz. The installer
smoke test once got "Token invalid" from the real ListenBrainz for a fake token meant for a fake api_url."""
import io, json, urllib.request

import pytest

from rormpc_tools import mbtag, musicdb


@pytest.fixture
def lb_config(tmp_path, monkeypatch):
    """Writes a listenbrainz-mpd config with the given [submission] lines; no config at all until called."""
    cfg = tmp_path / "listenbrainz-mpd" / "config.toml"
    monkeypatch.setattr(mbtag, "LB_CONFIGS", [cfg])
    monkeypatch.delenv("LISTENBRAINZ_TOKEN", raising=False)

    def write(*lines):
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text("[submission]\n" + "".join(l + "\n" for l in lines))
    return write


@pytest.mark.parametrize("lines, url", [
    ((), "https://api.listenbrainz.org/1/validate-token"),
    (('token = "t"', '#api_url = "http://127.0.0.1:9/"'), "https://api.listenbrainz.org/1/validate-token"),
    (('token = "t"', 'api_url = ""'), "https://api.listenbrainz.org/1/validate-token"),  # listenbrainz-mpd refuses ""
    (('token = "t"', 'api_url = "http://127.0.0.1:9"'), "http://127.0.0.1:9/1/validate-token"),
    (('token = "t"', 'api_url = "http://127.0.0.1:9//"'), "http://127.0.0.1:9/1/validate-token"),
    (('token = "t"', 'api_url = "http://127.0.0.1:9/lb/"'), "http://127.0.0.1:9/lb/1/validate-token"),
])
def test_lb_api(lb_config, lines, url):
    if lines:
        lb_config(*lines)
    assert mbtag.lb_api("/1/validate-token") == url


def test_token_and_api_url_from_the_same_config(lb_config):
    lb_config('api_url = "http://127.0.0.1:9/"', 'token = "fake-token"', 'genres_as_folksonomy = true')
    assert mbtag.lb_token() == "fake-token"
    assert mbtag.lb_api("/1/submit-listens") == "http://127.0.0.1:9/1/submit-listens"


@pytest.mark.parametrize("api_url, base", [(None, "https://api.listenbrainz.org"),
                                           ("http://127.0.0.1:9/", "http://127.0.0.1:9")])
def test_import_lb_asks_the_scrobblers_server(env, lb_config, monkeypatch, api_url, base):
    """musicdb update's ListenBrainz import: the token check and the listens both go to api_url."""
    lb_config('token = "fake-token"', *([f'api_url = "{api_url}"'] if api_url else []))
    monkeypatch.setattr(mbtag.settings, "LB_USER", None)
    env()
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/validate-token"):
            assert req.headers["Authorization"] == "Token fake-token"
            return io.BytesIO(json.dumps({"valid": True, "user_name": "someone"}).encode())
        return io.BytesIO(json.dumps({"payload": {"listens": []}}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    musicdb.import_lb(None)
    assert calls == [f"{base}/1/validate-token", f"{base}/1/user/someone/listens?count=100"]


def test_feedback_goes_to_the_scrobblers_server(env, lb_config, monkeypatch):
    lb_config('token = "fake-token"', 'api_url = "http://127.0.0.1:9/"')
    sent = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: sent.append(req.full_url) or io.BytesIO(b"{}"))
    assert musicdb.push_feedback(musicdb.db(), {"mb-rick": 1}) == 1
    assert sent == ["http://127.0.0.1:9/1/feedback/recording-feedback"]
