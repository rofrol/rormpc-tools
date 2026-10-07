"""musicdb lyrics translate: tekstowo.pl pages parsed, matched against our lyrics and aligned, offline. The pages
and lyrics here are synthetic (invented text in tekstowo.pl's markup); no fetched lyrics belong in this repo."""
import json

import pytest

from rormpc_tools import lyrics, translation as tr

ORIGINAL = ["Paper boats are sailing down the river", "Every evening when the lanterns glow", "",
            "Carry me home over the silver water", "Carry me home before the morning snow"]
POLISH = ["Papierowe łódki płyną w dół rzeki", "Każdego wieczoru, gdy świecą latarnie", "",
          "Zabierz mnie do domu po srebrnej wodzie", "Zabierz mnie do domu przed porannym śniegiem"]
OTHER = ["Neon signals flicker on the highway", "Engines humming underneath the bridge",
         "Nobody waits for trains at midnight", "Static on the radio again"]


def song_page(original, translation, machine=False):
    body = lambda lines: "<br />\n".join(lines)
    heading = "Tłumaczenie AI" if machine else "Tłumaczenie"
    return f"""<html><body>
<div class="song-text" id="songText" data-id="1"><h2 class="mb-2">Tekst piosenki: <span>X</span></h2>
<div class="inner-text">{body(original)}</div><a href="#">Edytuj</a></div>
<div class="tlumaczenie" id="songTranslation" data-id="1"><h2 class="mb-2">{heading}: <span>X</span></h2>
<div id="translation" class="id-2" data-id="2"><div class="inner-text {'auto-translation' if machine else ''}">{body(translation)}</div>
</div></div></body></html>"""


def search_page(results):
    rows = "".join(f"""<div class="box-przeboje"><div class="flex-group"><b>{i}.</b> <a href="{href}"
        class="title" title="{label}">{label} </a>
    </div>
    <div class="flex-group">
        <i title="tłumaczenie" class="icon i18 {icon}"></i></div></div>""" for i, (href, label, icon) in enumerate(results, 1))
    return f"<h2>Znalezione utwory:</h2>{rows}<h2>Znalezieni artyści:</h2>"


@pytest.fixture
def lyr(tmp_path, monkeypatch):
    d = tmp_path / "lyrics"
    d.mkdir()
    for mod in (lyrics, tr):
        monkeypatch.setattr(mod, "LYRICS", d)
    monkeypatch.setattr(lyrics, "INDEX", d / "index.json")
    pages, asked = {}, []

    def fake_get(url):
        asked.append(url)
        return pages.get(url)
    monkeypatch.setattr(tr, "get_html", fake_get)
    return d, pages, asked


def lrc(lines):
    return "[ar:A]\n[ti:T]\n" + "".join(f"[00:{i:02d}.00]{l}\n" for i, l in enumerate(lines))


def test_lrc_lines_number_timed_lines_like_rmpc():
    text = "[ar:A]\n[re:lrclib.net #1]\n\n[00:01.00]one\n[00:02.00]\n[00:03.00][00:09.00]chorus\nnot timed\n[00:04.00] two "
    assert tr.lrc_lines(text) == ["one", "", "chorus", "two"]


def test_split_lines_like_rust():
    assert tr.split_lines("a\r\nb\n\nc\n") == ["a", "b", "", "c"]
    assert tr.split_lines("a b") == ["a b"]  # str.splitlines would split here; Rust's lines() does not


def test_fnv_matches_reference():
    assert tr.fnv1a64("") == "cbf29ce484222325"
    assert tr.fnv1a64("a") == "af63dc4c8601ec8c"


def test_parse_song_human_and_machine():
    s = tr.parse_song(song_page(ORIGINAL, POLISH))
    assert s["original"] == ORIGINAL and s["translation"] == POLISH and not s["machine"]
    assert tr.parse_song(song_page(ORIGINAL, POLISH, machine=True))["machine"]
    assert tr.parse_song(song_page(ORIGINAL, []))["translation"] == []


def test_translated_line_by_line(lyr):
    d, pages, asked = lyr
    (d / "a").mkdir()
    (d / "a/song.lrc").write_text(lrc(ORIGINAL))
    pages[f"{tr.SITE}/the-band/paper-boats"] = song_page(ORIGINAL, POLISH)
    rec = tr.translate("a/song.mp3", "The Band", "Paper Boats (Remastered 2011)")
    assert asked == [f"{tr.SITE}/the-band/paper-boats"]  # one request when the usual URL holds the song
    assert rec["state"] == "translated" and rec["kind"] == "human" and rec["pairing"] == "line"
    assert rec["units"][0] == {"ids": [0], "text": [POLISH[0]]}
    assert [u["ids"] for u in rec["units"]] == [[0], [1], [3], [4]]
    assert rec["original_lang"] == "en" and rec["original_hash"] == tr.lines_hash(ORIGINAL)
    assert json.loads((d / "a/song.pl.json").read_text()) == rec
    assert (d / "a/song.lrc").read_text() == lrc(ORIGINAL)  # the lyrics stay untouched


def test_wrong_song_is_rejected_then_search_finds_the_right_one(lyr):
    d, pages, asked = lyr
    (d / "song.txt").write_text("\n".join(ORIGINAL) + "\n")
    pages[f"{tr.SITE}/the-band/paper-boats"] = song_page(OTHER, ["Neonowe sygnały"])
    pages[f"{tr.SITE}/szukaj?search-query=The+Band+Paper+Boats"] = search_page([
        ("/someone-else/paper-boats", "Someone Else - Paper Boats", "icon_pl"),
        ("/the-band/paper-boats", "The Band - Paper Boats", "icon_pl"),
        ("/the-band/paper-boats-live", "The Band - Paper Boats (Live)", "icon_pl")])
    pages[f"{tr.SITE}/the-band/paper-boats-live"] = song_page(ORIGINAL, POLISH)
    rec = tr.translate("song.mp3", "The Band", "Paper Boats")
    assert rec["state"] == "translated" and rec["url"].endswith("/the-band/paper-boats-live")
    assert f"{tr.SITE}/someone-else/paper-boats" not in asked  # another artist's song is never fetched


def test_only_other_lyrics_is_a_mismatch(lyr):
    d, pages, _ = lyr
    (d / "song.txt").write_text("\n".join(ORIGINAL) + "\n")
    pages[f"{tr.SITE}/the-band/paper-boats"] = song_page(OTHER, ["Neonowe sygnały"])
    rec = tr.translate("song.mp3", "The Band", "Paper Boats")
    assert rec["state"] == "mismatch" and "units" not in rec


def test_no_translation_and_not_found_are_cached(lyr):
    d, pages, _ = lyr
    (d / "song.txt").write_text("\n".join(ORIGINAL) + "\n")
    pages[f"{tr.SITE}/the-band/paper-boats"] = song_page(ORIGINAL, [])
    assert tr.translate("song.mp3", "The Band", "Paper Boats")["state"] == "none"
    assert tr.load("song.mp3")["state"] == "none"
    pages.clear()
    assert tr.translate("song.mp3", "The Band", "Paper Boats")["state"] == "not_found"


def test_machine_translation_is_labelled(lyr):
    d, pages, _ = lyr
    (d / "song.txt").write_text("\n".join(ORIGINAL) + "\n")
    pages[f"{tr.SITE}/the-band/paper-boats"] = song_page(ORIGINAL, POLISH, machine=True)
    assert tr.translate("song.mp3", "The Band", "Paper Boats")["kind"] == "machine"


def test_polish_original_gets_no_translation_and_no_request(lyr):
    d, _, asked = lyr
    (d / "song.txt").write_text("\n".join(POLISH) + "\n")
    rec = tr.translate("song.mp3", "Zespół", "Łódki")
    assert rec["state"] == "original_pl" and asked == []


def test_manual_language_override_wins(lyr):
    d, pages, asked = lyr
    (d / "song.txt").write_text("\n".join(POLISH) + "\n")
    tr.set_lang("song.mp3", "cs")
    pages[f"{tr.SITE}/zespol/lodki"] = song_page(POLISH, ORIGINAL)
    rec = tr.translate("song.mp3", "Zespół", "Łódki")
    assert rec["original_lang"] == "cs" and rec["original_lang_manual"] and rec["state"] == "translated"
    assert tr.set_lang("song.mp3", "auto")["original_lang"] == "pl"


def test_mine_is_never_overwritten(lyr):
    d, pages, asked = lyr
    (d / "song.txt").write_text("\n".join(ORIGINAL) + "\n")
    mine = {"version": 1, "lang": "pl", "state": "translated", "kind": "mine", "source": "mine", "pairing": "none",
            "units": [{"ids": [0, 1, 2, 3, 4], "text": ["moje"]}]}
    tr.save("song.mp3", mine)
    with pytest.raises(RuntimeError):
        tr.translate("song.mp3", "The Band", "Paper Boats")
    assert tr.load("song.mp3") == mine and asked == []


def test_instrumental_without_lyrics_is_cached(lyr):
    _, _, asked = lyr
    assert tr.translate("song.mp3", "The Band", "Paper Boats", "instrumental")["state"] == "instrumental"
    assert tr.translate("other.mp3", "The Band", "Other") is None and asked == []


def test_repeated_chorus_written_once_maps_every_repeat():
    ours = ORIGINAL + ["", "Carry me home over the silver water", "Carry me home before the morning snow"]
    pairing, units = tr.align(ours, ORIGINAL, POLISH)
    assert pairing == "line"
    assert {u["ids"][0]: u["text"][0] for u in units}[6] == POLISH[3]


def test_backing_vocals_in_parentheses_still_match():
    ours = ORIGINAL + ["", "Carry me home (carry me home) over the silver water"]
    pairing, units = tr.align(ours, ORIGINAL, POLISH)
    assert {u["ids"][0]: u["text"][0] for u in units}[6] == POLISH[3]


def test_merged_verses_align_by_stanza():
    their_pl = ["Papierowe łódki płyną w dół rzeki każdego wieczoru", "",
                "Zabierz mnie do domu", "po srebrnej wodzie", "przed porannym śniegiem"]
    pairing, units = tr.align(ORIGINAL, ORIGINAL, their_pl)
    assert pairing == "stanza"
    assert units == [{"ids": [0, 1], "text": [their_pl[0]]}, {"ids": [3, 4], "text": their_pl[2:]}]


def test_stanzas_of_equal_length_still_pair_line_by_line():
    ours = ORIGINAL + ["", "Carry me home over the silver water"]
    their_orig = ORIGINAL + ["", "Far away", "Carry me home over the silver water"]
    their_pl = POLISH[:2] + ["", "Zabierz mnie do domu po srebrnej wodzie i przed porannym śniegiem", "",
                             "Daleko", "Zabierz mnie do domu po srebrnej wodzie"]
    pairing, units = tr.align(ours, their_orig, their_pl)
    assert pairing == "stanza"
    assert units[0] == {"ids": [0], "text": [POLISH[0]], "line": True}
    assert units[2] == {"ids": [3, 4], "text": [their_pl[3]]}
    assert units[3] == {"ids": [6], "text": ["Zabierz mnie do domu po srebrnej wodzie"], "line": True}


def test_partly_matched_lyrics_still_pair_line_by_line():
    ours = ORIGINAL + ["", "A bridge the page leaves out", "Another line it does not have"]
    pairing, units = tr.align(ours, ORIGINAL, POLISH)
    assert pairing == "line" and [u["ids"] for u in units] == [[0], [1], [3], [4]]


def test_footnotes_after_the_translation_are_left_out():
    pairing, units = tr.align(ORIGINAL, ORIGINAL, POLISH + ["", "* łódki: małe łodzie", "", "** latarnie: lampy"])
    assert pairing == "line" and units[-1] == {"ids": [4], "text": [POLISH[4]]}


def test_unalignable_translation_is_one_block():
    pairing, units = tr.align(ORIGINAL, ORIGINAL, ["jedna", "", "dwie", "", "trzy"])
    assert pairing == "none" and units == [{"ids": [0, 1, 2, 3, 4], "text": ["jedna", "", "dwie", "", "trzy"]}]


def test_search_ranking_prefers_exact_title_and_skips_other_artists():
    results = tr.parse_search(search_page([
        ("/x/paper-boats-live", "The Band - Paper Boats (Live)", "icon_pl"),
        ("/y/paper-boats", "Other Band - Paper Boats", "icon_pl"),
        ("/x/paper-boats", "The Band - Paper Boats", ""),
        ("/x/paper-boats-ai", "The Band - Paper Boats", "icon_ai_trans")]))
    assert results[3]["translation"] == "machine" and results[2]["translation"] is None
    ranked = [r["url"].removeprefix(tr.SITE) for r in tr.rank_results(results, "The Band", "Paper Boats")]
    assert ranked == ["/x/paper-boats-ai", "/x/paper-boats", "/x/paper-boats-live"]


def test_cli_translate_prints_the_outcome(lyr, env, capsys):
    d, pages, _ = lyr
    env([{"file": "song.mp3", "artist": "The Band", "title": "Paper Boats"}])
    (d / "song.txt").write_text("\n".join(ORIGINAL) + "\n")
    pages[f"{tr.SITE}/the-band/paper-boats"] = song_page(ORIGINAL, POLISH)
    lyrics.main(["translate", "song.mp3"])
    assert "line by line" in capsys.readouterr().out
