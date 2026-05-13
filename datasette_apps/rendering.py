from __future__ import annotations

import html
import re


def _csp_meta(csp):
    return (
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{html.escape(csp, quote=True)}">'
    )


def build_app_srcdoc(source, csp):
    source = source or ""
    meta = _csp_meta(csp)
    head_match = re.search(r"<head\b[^>]*>", source, flags=re.IGNORECASE)
    if head_match:
        return source[: head_match.end()] + meta + source[head_match.end() :]

    html_match = re.search(r"<html\b[^>]*>", source, flags=re.IGNORECASE)
    if html_match:
        return source[: html_match.end()] + f"<head>{meta}</head>" + source[html_match.end() :]

    doctype_match = re.match(r"\s*<!doctype html\s*>", source, flags=re.IGNORECASE)
    if doctype_match:
        return (
            source[: doctype_match.end()]
            + f"<html><head>{meta}</head><body>"
            + source[doctype_match.end() :]
            + "</body></html>"
        )

    return f"<html><head>{meta}</head><body>{source}</body></html>"
