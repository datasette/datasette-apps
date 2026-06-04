from html.parser import HTMLParser

import pytest

from datasette_apps.rendering import build_app_srcdoc


class ParsedTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def parsed_tags(html):
    parser = ParsedTags()
    parser.feed(html)
    return parser.tags


def is_csp_meta(tag):
    name, attrs = tag
    return (
        name == "meta"
        and attrs.get("http-equiv") == "Content-Security-Policy"
        and attrs.get("content") == "default-src 'none';"
    )


def is_bridge_script(tag):
    name, attrs = tag
    return name == "script" and attrs.get("id") == "datasette-apps-bridge"


CSP_BREAK_CASES = [
    pytest.param(
        "<!DOCTYPE html><html><head><title>Hello</title></head>"
        '<body><script id="attacker"></script></body></html>',
        id="baseline-complete-document",
    ),
    pytest.param(
        '<html><body><script id="attacker"></script></body></html>',
        id="baseline-html-without-head",
    ),
    pytest.param(
        '<!DOCTYPE html><h1>Hello</h1><script id="attacker"></script>',
        id="baseline-doctype-fragment",
    ),
    pytest.param(
        '<h1>Hello</h1><script id="attacker"></script>',
        id="baseline-bare-fragment",
    ),
    pytest.param(
        "<!-- <head> -->\n<script>steal()</script>\n<head></head>",
        id="A-comment-fake-head",
    ),
    pytest.param(
        "<title><head></title>\n<script>steal()</script>\n<head></head>",
        id="A-title-fake-head",
    ),
    pytest.param(
        "<textarea><head></textarea>\n<script>steal()</script>\n<head></head>",
        id="A-textarea-fake-head",
    ),
    pytest.param(
        '<script>var x = "<head>"; steal();</script>\n<head></head>',
        id="A-script-text-fake-head",
    ),
    pytest.param(
        "<style>/* <head> */</style>\n<script>steal()</script>\n<head></head>",
        id="A-style-fake-head",
    ),
    pytest.param(
        "<noscript><head></noscript>\n<script>steal()</script>\n<head></head>",
        id="A-noscript-fake-head",
    ),
    pytest.param(
        "<xmp><head></xmp>\n<script>steal()</script>\n<head></head>",
        id="A-xmp-fake-head",
    ),
    pytest.param(
        '<div data-note="<head>">\n<script>steal()</script>\n<head></head></div>',
        id="A-attribute-fake-head",
    ),
    pytest.param(
        "<!--[if IE]><head><![endif]-->\n<script>steal()</script>\n<head></head>",
        id="A-conditional-comment-fake-head",
    ),
    pytest.param(
        "<plaintext><head>\n<script>steal()</script>",
        id="A-plaintext-fake-head",
    ),
    pytest.param(
        "<script>steal()</script>\n<head></head>",
        id="B-script-before-head",
    ),
    pytest.param(
        '<img src=x onerror="steal()">\n<head></head>',
        id="B-img-onerror-before-head",
    ),
    pytest.param(
        '<meta http-equiv="refresh" content="0;url=//evil.example">\n<head></head>',
        id="B-meta-refresh-before-head",
    ),
    pytest.param(
        '<base href="//evil.example/">\n<head></head>',
        id="B-base-before-head",
    ),
    pytest.param(
        '<link rel="stylesheet" href="//evil.example/x.css">\n<head></head>',
        id="B-stylesheet-before-head",
    ),
    pytest.param(
        "<svg><script>steal()</script></svg>\n<head></head>",
        id="B-svg-script-before-head",
    ),
    pytest.param(
        '<iframe src="javascript:steal()"></iframe>\n<head></head>',
        id="B-iframe-javascript-before-head",
    ),
    pytest.param(
        '<body onload="steal()">\n<head></head>',
        id="B-body-onload-before-head",
    ),
    pytest.param(
        '<input autofocus onfocus="steal()">\n<head></head>',
        id="B-input-autofocus-before-head",
    ),
    pytest.param(
        '<head data-x="a>b"></head>',
        id="C-head-attribute-greater-than",
    ),
    pytest.param(
        '<html data-x="a>b"></html>',
        id="C-html-attribute-greater-than",
    ),
    pytest.param(
        '<head title=">"></head>',
        id="C-head-title-greater-than",
    ),
    pytest.param(
        '<head data-json=\'{"k":">"}\'></head>',
        id="C-head-json-greater-than",
    ),
    pytest.param(
        '<head data-x="-->"></head>',
        id="C-head-comment-end-attribute",
    ),
    pytest.param(
        '<head data-tpl="if (a>0)"></head>',
        id="C-head-template-expression",
    ),
    pytest.param(
        '<head aria-label="more > less"></head>',
        id="C-head-aria-greater-than",
    ),
    pytest.param(
        "<HEAD></HEAD>",
        id="D-uppercase-head",
    ),
    pytest.param(
        "<Head\n  lang=\"en\"\n></Head>",
        id="D-multiline-head",
    ),
    pytest.param(
        "<head/>\n<script>steal()</script>",
        id="D-self-closing-head",
    ),
    pytest.param(
        "<head ></head>",
        id="D-head-space",
    ),
    pytest.param(
        "<head><head>\n<script>steal()</script>",
        id="D-double-head",
    ),
    pytest.param(
        "<HTML><HEAD></HEAD></HTML>",
        id="D-uppercase-html-head",
    ),
    pytest.param(
        "<!-- <html> -->\n<script>steal()</script>\n<html></html>",
        id="E-comment-fake-html",
    ),
    pytest.param(
        "<title><html></title>\n<script>steal()</script>\n<html></html>",
        id="E-title-fake-html",
    ),
    pytest.param(
        '<html data-x="a>b"><body><script>steal()</script></body></html>',
        id="E-html-attribute-greater-than",
    ),
    pytest.param(
        "<script>steal()</script>\n<html><body>BAD</body></html>",
        id="E-script-before-html",
    ),
    pytest.param(
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0//EN">\n'
        "<script>steal()</script>\n<head></head>",
        id="F-public-doctype",
    ),
    pytest.param(
        '<!DOCTYPE html SYSTEM "about:legacy-compat">\n'
        "<script>steal()</script>\n<html></html>",
        id="F-system-doctype",
    ),
    pytest.param(
        "<!doctype  html >\n<script>steal()</script>\n<head></head>",
        id="F-extra-space-doctype",
    ),
    pytest.param(
        "<!-- comment first -->\n<!doctype html>\n<script>steal()</script>\n<head></head>",
        id="F-comment-before-doctype",
    ),
    pytest.param(
        '<!-- <head> -->\n<img src=x onerror="steal()">\n<head data-x="a>b"></head>',
        id="G-comment-img-attribute-head",
    ),
    pytest.param(
        '<title><head></title>\n<base href="//evil.example/">\n<head></head>',
        id="G-title-base-head",
    ),
    pytest.param(
        '<!DOCTYPE html PUBLIC "x">\n<script>var s="<head>";</script>\n'
        "<img src=x onerror=steal()>\n<head title=\">\"></head>",
        id="G-public-doctype-script-img-attribute-head",
    ),
    pytest.param(
        "<plaintext>\n<head>\n<script>this is all text but the regex already matched the head above</script>",
        id="G-plaintext-head-script",
    ),
]


@pytest.mark.parametrize("source", CSP_BREAK_CASES)
def test_srcdoc_security_tags_are_first_real_tags(source):
    srcdoc = build_app_srcdoc(
        source,
        "default-src 'none';",
        '<script id="datasette-apps-bridge"></script>',
    )
    tags = parsed_tags(srcdoc)

    assert len(tags) >= 2
    assert is_csp_meta(tags[0])
    assert is_bridge_script(tags[1])
