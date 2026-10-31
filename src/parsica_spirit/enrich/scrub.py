#!/usr/bin/env python3
"""
Parsica - content scrubber (shared leaf).

Redacts credentials/secrets (always) and PII (configurable) from any text BEFORE it is
stored. Lives in the leaf so BOTH the Parsica Spirit Context facade and the engine
(MemorySystem) run the identical scrub-before-store gate - there is no write path around it.

Design rules:
  * NEVER entropy-redact whole tokens. Transcripts are full of 40-char git SHAs,
    file hashes, base64 artifacts. Secrets are caught by SPECIFIC token shapes +
    key-name/value-SHAPE rules only.
  * Recall: catch opaque-valued secrets in assignment context regardless of the
    surrounding key name (access_token=, AccountKey=, "apiKey": "...", DB URLs),
    including values followed by trailing text on the same line.
  * Precision: do NOT gut ordinary code/config. A secret-named key with a CODE
    value (call, attribute access, path) or a config-enum value (AES-256-GCM,
    us-east-1, v1.2.3) is left intact. The value's SHAPE + position decide.
  * Placeholders are not secrets - skip <...>, ${VAR}, $VAR, ***, your-...

Public API:
    scrub(text, *, scrub_pii=True) -> (clean_text, n_redactions)
    looks_clean(text) -> bool        # quick "does this contain a secret?" probe
"""
import re
import unicodedata

__all__ = ["scrub", "looks_clean", "REDACTION", "scrub_write"]

REDACTION = "[REDACTED:{label}]"
_MAXVAL = 8192  # upper length bound on a captured/judged secret value


def _mask(label):
    return REDACTION.format(label=label)


# ---- placeholder detection (these are NOT secrets) -------------------------
_PLACEHOLDER = re.compile(
    r"^(?:[<\[{].*[>\]}]|\$\{?\w+\}?|%\w+%|\*{3,}|x{3,}|\.{3,}|"
    r"your[-_].*|my[-_].*|example.*|changeme|change[-_]?me|redacted|placeholder|"
    r"none|null|true|false|test|dummy|sample|fake|todo|not[-_].*|tbd|"
    r"required|optional|enabled|disabled|unset|default|localhost|empty|auto)$",
    re.IGNORECASE,
)


def _is_placeholder(val):
    v = val.strip().strip("'\"")
    if not v or len(v) < 6:
        return True
    if _PLACEHOLDER.match(v):
        return True
    if v[0] in "$<%" or v.startswith("${"):
        return True
    return False


# ---- secret token patterns (specific prefixes / shapes) --------------------
# Left boundary: not glued to a preceding alphanumeric (catches separator- and
# underscore-glued pastes; a non-consuming lookbehind so it never eats a char).
_NB = r"(?<![A-Za-z0-9])"
_TOKEN_PATTERNS = [
    ("anthropic_key", re.compile(_NB + r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(_NB + r"sk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(_NB + r"(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("gcp_api_key", re.compile(_NB + r"AIza[0-9A-Za-z_\-]{35}(?![A-Za-z0-9])")),
    ("github_pat", re.compile(_NB + r"github_pat_[A-Za-z0-9_]{22,}")),
    ("github_token", re.compile(_NB + r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack_token", re.compile(_NB + r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("slack_app_token", re.compile(_NB + r"xapp-[0-9]-[A-Za-z0-9\-]{20,}")),
    ("stripe_key", re.compile(_NB + r"(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{16,}")),
    ("stripe_webhook_secret", re.compile(_NB + r"whsec_[0-9a-zA-Z]{16,}")),
    ("gitlab_token", re.compile(_NB + r"(?:glpat|gloas|gldt|glrt|glrtr|glcbt|glptt|glsoat)-[A-Za-z0-9_\-]{16,}")),
    ("google_oauth", re.compile(_NB + r"ya29\.[0-9A-Za-z_\-]{20,}")),
    ("google_client_secret", re.compile(_NB + r"GOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("npm_token", re.compile(_NB + r"npm_[A-Za-z0-9]{36}(?![A-Za-z0-9])")),
    ("pypi_token", re.compile(_NB + r"pypi-AgE[A-Za-z0-9_\-]{16,}")),
    ("hf_token", re.compile(_NB + r"hf_[A-Za-z0-9]{34,}")),
    ("sendgrid_key", re.compile(_NB + r"SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}(?![A-Za-z0-9])")),
    ("digitalocean_token", re.compile(_NB + r"dop_v1_[a-f0-9]{64}(?![a-f0-9])")),
    ("docker_pat", re.compile(_NB + r"dckr_pat_[A-Za-z0-9_\-]{20,}")),
    # Telegram bot token - no left boundary so the in-URL form (.../bot<token>/...) is caught.
    ("telegram_bot_token", re.compile(r"[0-9]{8,10}:AA[A-Za-z0-9_\-]{32,33}(?![A-Za-z0-9_\-])")),
    ("discord_token", re.compile(_NB + r"[MNO][A-Za-z0-9]{23}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}")),
    ("jwt", re.compile(_NB + r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+")),
    ("discord_webhook", re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_\-]+")),
    ("bearer_token", re.compile(r"(?<![A-Za-z0-9])Bearer\s+(?=[A-Za-z0-9._\-]*[A-Z0-9])[A-Za-z0-9._\-]{20,}")),
    # scheme://[user]:password@host  - any scheme, optional user, always-on. The
    # (?!\d+@) keeps host:port@host infra notation (redis://cache:6379@primary) intact.
    ("conn_string_cred", re.compile(_NB + r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]*:(?!\d+@)[^/\s@]{3,}@")),
]

# `(?: BLOCK)?` so PGP/GPG armored private keys (-----BEGIN PGP PRIVATE KEY BLOCK-----)
# match alongside RSA/EC/OPENSSH/ENCRYPTED/PKCS8/DSA. PUBLIC KEY armor is left intact.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)

# Signed-URL credential query params - keep the URL shape, redact only the credential-bearing
# value (AWS presigned X-Amz-Signature, Azure SAS sig, generic signature=...).
_SIGNED_URL_PARAM = re.compile(r"(?i)([?&](?:sig|signature|x-amz-signature)=)([^&#\s\"']{16,})")

# Authorization headers - case-INSENSITIVE on the scheme word only; the value is judged by
# a callback (placeholder check + case-sensitive entropy) so prose like "Bearer required"
# survives while real header-shaped credentials are redacted.
_AUTH_HEADER = re.compile(
    r"\bAuthorization:[ \t]*(?:(?P<scheme>Bearer|Basic|Token|ApiKey|Digest)[ \t]+)?(?P<val>[A-Za-z0-9._/+=\-]{8,})",
    re.IGNORECASE,
)
# a pure lowercase snake_case / hyphenated identifier (x_internal_request_id,
# middleware-request-validator) - a code symbol, NOT an opaque credential.
_AUTH_IDENT = re.compile(r"[a-z0-9]+(?:[_\-][a-z0-9]+)+$")


def _auth_header_value_is_secret(val):
    if _is_placeholder(val):
        return False
    # The literal `Authorization:` header name is strong credential context, so a long opaque
    # value (>=20 chars) is a credential regardless of scheme word OR case - gateways/proxies
    # log "Authorization: <opaque-token>" with no scheme. But a long pure-lowercase snake_case /
    # hyphenated IDENTIFIER (no digit/symbol/case-mix) is a code symbol, not a token (priority
    # #2), so length alone must not eat it. Placeholders already bailed.
    if len(val) >= 20 and not _AUTH_IDENT.match(val):
        return True
    has_digit = any(c.isdigit() for c in val)
    has_sym = any(c in "+/=" for c in val)
    has_mix = any(c.isupper() for c in val) and any(c.islower() for c in val)
    return has_digit or has_sym or has_mix

# ---- assignment scrubbing: key-name + value-SHAPE driven -------------------
# A key "counts" as secret-bearing if a delimited segment matches one of these
# compounds. Bare 'key' is deliberately EXCLUDED (cache_key/sort_key/primary_key
# are ordinary code); only specific *_key compounds count.
_SECRET_KEY_COMPOUNDS = (
    r"api[_-]?key", r"access[_-]?key", r"secret[_-]?key", r"account[_-]?key",
    r"subscription[_-]?key", r"consumer[_-]?key", r"encryption[_-]?key",
    r"private[_-]?key", r"signing[_-]?key", r"auth[_-]?key",
    r"client[_-]?secret", r"auth[_-]?token", r"session[_-]?token",
    r"refresh[_-]?token", r"access[_-]?token", r"api[_-]?token",
    r"bot[_-]?token", r"app[_-]?token", r"bearer[_-]?token",
    r"secret", r"password", r"passwd", r"passphrase",
)
_SECRET_KEY_RE = re.compile(
    r"^(?:[a-z0-9]+[_\-])*(?:" + "|".join(_SECRET_KEY_COMPOUNDS) + r")(?:[_\-][a-z0-9]+)?$",
    re.IGNORECASE,
)
_SECRET_SUFFIX_RE = re.compile(
    r"(?:^|[_\-])(?:token|secret|password|passwd|passphrase)$", re.IGNORECASE
)
# a key ending in one of these is a REFERENCE (the path/name/version OF a secret),
# not the secret value itself -> never redact its value. NOTE: 'id' and 'header'
# are deliberately NOT here - secret_id / private_key_id / apikey_header are real
# credentials, not references (their value-shape gate still guards precision).
_KEY_REF_SUFFIX_RE = re.compile(
    r"(?:^|[_\-])(?:path|file|files|dir|directory|name|url|uri|"
    r"regex|pattern|count|len|length|size|type|field|column|var|env|prefix|suffix|"
    r"enabled|disabled|required|optional|ttl|expiry|expires|format|encoding|algo|"
    r"algorithm|hash|hint|label|kind|class|scope|list|map|set|index|store|provider|"
    r"manager|service|rotation|policy|mode|status|state|version|revision|rev)$",
    re.IGNORECASE,
)


def _is_secret_key(key):
    k = (key or "").strip()
    if not k:
        return False
    if _KEY_REF_SUFFIX_RE.search(k):
        return False
    if _SECRET_SUFFIX_RE.search(k):
        return True
    return bool(_SECRET_KEY_RE.match(k))


_ATTR_ACCESS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]+)+$")
_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SEG_RE = re.compile(r"[-._]")


def _has_entropy(v):
    return (any(c.isdigit() for c in v) or any(c.isupper() for c in v)
            or any(c in "+/=" for c in v))


def _is_config_enum(v):
    """Cipher names / regions / versions / short enums: every '-._'-segment is short
    (AES-256-GCM, us-east-1, v1.2.3-beta). A long, many-segment value is an opaque
    grouped secret, NOT an enum, so it is not whitelisted."""
    segs = [s for s in _SEG_RE.split(v) if s]
    if not segs or not all(len(s) <= 8 for s in segs):
        return False
    if len(v) >= 24 and len(segs) >= 6:
        return False
    return True


_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_PATHSEG_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _is_snake_identifier(v):
    """lowercase words joined by underscores -> a code identifier / phrase, not a secret."""
    return bool(_SNAKE_RE.match(v))


def _looks_like_path(v):
    """A slash-delimited reference (Vault KV path secret/data/prod/db) - every segment a
    short word, no long opaque base64 blob - is a path, not the secret itself."""
    if "/" not in v:
        return False
    segs = [s for s in v.split("/") if s]
    return bool(segs) and all(len(s) <= 16 and _PATHSEG_RE.match(s) for s in segs)


def _looks_secret_quoted(v, explicit=False):
    """A quoted RHS after a secret key. `explicit` = the key is an unambiguous
    password/passphrase/secret/token name, so an all-lowercase passphrase counts."""
    v = v.strip()
    if not (6 <= len(v) <= _MAXVAL):
        return False
    if _is_placeholder(v):
        return False
    if any(c.isspace() for c in v):  # secrets have no internal whitespace
        return False
    if v[0] in "/~" or _WIN_PATH_RE.match(v):
        return False
    if _looks_like_path(v):          # secret/data/prod/db -> a path reference
        return False
    if _is_config_enum(v):           # AES-256-GCM / us-east-1 / v1.2.3-beta
        return False
    if _is_snake_identifier(v):      # the_function_returns_early -> code/phrase
        return False
    if explicit:                     # explicit secret key + delimited value -> trust it
        return True
    if not _has_entropy(v):          # plain dictionary words -> not a secret
        return False
    return True


def _looks_secret_value(v, standalone=True, explicit=False):
    """An UNQUOTED RHS token. Stricter than quoted: reject code/path/hash shapes. When
    not standalone (trailing prose follows), require a digit/symbol so camelCase prose
    words aren't masked. `explicit` lets an all-lowercase passphrase under an explicit
    password/secret key be caught (correcthorsebatterystaple)."""
    v = v.strip().strip("'\"")
    if len(v) < 8 or len(v) > _MAXVAL:
        return False
    if _is_placeholder(v):
        return False
    if any(c.isspace() for c in v):
        return False
    if v[0] in "/~." or _WIN_PATH_RE.match(v):  # path
        return False
    if "(" in v or ")" in v:                     # code call
        return False
    if _ATTR_ACCESS_RE.match(v):                 # attribute access -> code
        return False
    if _looks_like_path(v):                      # secret/data/prod/db -> path reference
        return False
    if _is_config_enum(v):                       # AES-256-GCM etc.
        return False
    if _is_snake_identifier(v):                  # the_function_returns_early -> code
        return False
    # explicit secret key + an all-lowercase value: catch a real passphrase
    # (correcthorsebatterystaple, supersecretword) but NOT an ordinary prose word that follows
    # a literal password:/secret: in notes (yesterday, configuration, authentication). Length
    # is the only separator with entropy-off - require 15+, above the common prose words.
    if explicit and re.match(r"^[a-z]{15,}$", v):
        return True
    has_digit = any(c.isdigit() for c in v)
    has_symbol = any(c in "+/=" for c in v)
    if has_digit or has_symbol:
        return True
    if standalone and any(c.isupper() for c in v):
        # a bare PascalCase/camelCase word with no separator/digit is a code
        # identifier (AuthTokenProvider, OAuthClient), not an opaque secret
        if re.match(r"^[A-Za-z][A-Za-z0-9]*$", v):
            return False
        return True
    return False


# key (optional quotes) sep value (quoted)
_ASSIGN_QUOTED = re.compile(
    r"(?P<kq>[\"']?)(?P<key>[A-Za-z][\w\-]{0,40})(?P=kq)"
    r"(?P<sep>\s*(?:\|\|=|\?\?=|\+=|=>|:=|[:=])\s*)"
    r"(?P<vq>[\"'])(?P<val>[^\"'\r\n]{1," + str(_MAXVAL) + r"})(?P=vq)"
)
# key (optional quotes) sep value (bare token, self-terminating at ws/terminator). The
# trailing lookahead accepts a closing quote/backtick as a terminator (mirroring the value
# class) so a quote-wrapped credential - curl -H 'X: api_key=<secret>' - is reachable.
_ASSIGN_BARE = re.compile(
    r"(?P<kq>[\"']?)(?P<key>[A-Za-z][\w\-]{0,40})(?P=kq)"
    r"(?P<sep>\s*(?:\|\|=|\?\?=|\+=|=>|:=|[:=])\s*)"
    r"(?P<val>(?:\[REDACTED(?::[^\]]*)?\]|[^\s\"'`;,)\]}&?\r\n]){6," + str(_MAXVAL) + r"})(?=[\s\"'`;,)\]}&?]|$)"
)

# A secret-key assignment that a NON-SECRET prefix shadowed in the bare pass: in
# "DEBUG: api_key = <secret>" the bare value class stops at the space, so the outer non-secret
# key ('DEBUG') eats the inner key NAME ('api_key') and the value past the space is never seen.
# This pass is anchored on the secret-key NAME itself (prefix-independent), catching the value
# the bare pass missed. It runs last; values already redacted (starting "[REDACTED") are skipped.
_SHADOW_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<key>(?:[A-Za-z0-9]+[_\-])*(?:" + "|".join(_SECRET_KEY_COMPOUNDS) + r")(?:[_\-][A-Za-z0-9]+)?)"
    r"(?P<sep>\s*(?:\|\|=|\?\?=|\+=|=>|:=|[:=])\s*)"
    r"(?P<val>(?:\[REDACTED(?::[^\]]*)?\]|[^\s\"'`;,)\]}&?\r\n]){6," + str(_MAXVAL) + r"})(?=[\s\"'`;,)\]}&?]|$)",
    re.IGNORECASE,
)


_VAL_MARKER_SPLIT = re.compile(r"(\[REDACTED(?::[^\]]*)?\]?)")   # keep markers when splitting a value


def _value_with_marker_redacted(val, counter):
    """A captured value that ALREADY contains a redaction marker: redact any LIVE secret material
    around the marker (a real secret glued before/after an in-place redaction) while PRESERVING the
    marker(s) - never re-mask a marker (no double-count) and never leave a stray bracket. Returns
    the rebuilt value, or None when there is no live secret (caller leaves the span untouched)."""
    found = False
    out = []
    for seg in _VAL_MARKER_SPLIT.split(val):
        if not seg:
            continue
        if seg.startswith("[REDACTED"):
            out.append(seg)                     # an existing marker - keep verbatim
            continue
        core = seg.strip(" =:;,\t./~")          # strip a key-separator / glue wrapper (incl '.'/'/' glue)
        if core and _looks_secret_value(core, standalone=True, explicit=True):
            out.append(seg.replace(core, _mask("secret"), 1))
            found = True
        else:
            out.append(seg)
    if not found:
        return None
    counter[0] += 1
    return "".join(out)


# A live secret glued to an in-place marker via a value-TERMINATOR char (',' ';' ')' ']' '}' '&'
# '?') - the assignment value class STOPS at the terminator, so a trailing opaque blob escapes the
# marker handler above (two concatenated secrets: `password=AKIA...[id-redacted],wJalrXU...`). Catch
# <marker><terminator(s)><bare-opaque-blob>. The blob class excludes '='/':' so a following
# key=value (';Host=db') is NOT a blob (left alone), and _looks_secret_value gates it so a snake_case
# id / config-enum / path / plain word glued after a marker survives.
_MARKER_SEP_GLUE = re.compile(
    r"(\[REDACTED(?::[^\]]*)?\])([;,)\]}&?]+)([^\s\"'`;,)\]}&?=:\r\n]{8," + str(_MAXVAL) + r"})"
)


def _redact_marker_glue(text, counter):
    def repl(m):
        if _looks_secret_value(m.group(3), standalone=True, explicit=False):
            counter[0] += 1
            return m.group(1) + m.group(2) + _mask("secret")
        return m.group(0)
    return _MARKER_SEP_GLUE.sub(repl, text)


def _redact_assignments(text, counter):
    def repl_quoted(m):
        val = m.group("val")
        key = m.group("key")
        if "[REDACTED" in val:                  # already-redacted: mask only LIVE residue, keep markers
            # gate on a secret key - a marker's mere presence must NOT flip a NON-secret key's value
            # (a git SHA / long word glued beside a marker under commit_note/log_line must survive).
            if not _is_secret_key(key):
                return m.group(0)
            rebuilt = _value_with_marker_redacted(val, counter)
            if rebuilt is None:
                return m.group(0)
            kq, vq = m.group("kq"), m.group("vq")
            return f"{kq}{key}{kq}{m.group('sep')}{vq}{rebuilt}{vq}"
        # every key in _SECRET_KEY_COMPOUNDS is an UNAMBIGUOUS secret key (api_key, secret_key,
        # auth_key, client_secret, *_token, password...), so an all-lowercase quoted value under
        # one is the secret - treat it as explicit. Config-enum/path/placeholder gates in
        # _looks_secret_quoted still spare AES-256-GCM / us-east-1 / not-configured-yet.
        if _is_secret_key(key) and _looks_secret_quoted(val, explicit=True):
            counter[0] += 1
            kq, vq = m.group("kq"), m.group("vq")
            return f"{kq}{key}{kq}{m.group('sep')}{vq}{_mask('secret')}{vq}"
        return m.group(0)

    def repl_bare(m):
        val = m.group("val")
        key, kq, sep = m.group("key"), m.group("kq"), m.group("sep")
        if _is_secret_key(key):
            if "[REDACTED" in val:              # already-redacted: mask only LIVE residue, keep markers
                rebuilt = _value_with_marker_redacted(val, counter)
                return f"{kq}{key}{kq}{sep}{rebuilt}" if rebuilt is not None else m.group(0)
            rest = m.string[m.end():]
            line_rest = rest.split("\n", 1)[0].strip()
            standalone = (line_rest == "" or line_rest[:1] in ";,)]}")
            if _looks_secret_value(val, standalone=standalone, explicit=True):
                counter[0] += 1
                return f"{kq}{key}{kq}{sep}{_mask('secret')}"
        # a non-secret (or non-opaque) PREFIX key can shadow an inner key=value, because the
        # bare-value class admits '='/':' (Cookie: session_token=<secret>, curl -H 'X: k=<s>',
        # DEBUG: api_key=<secret>) - re-scan the captured value for an inner assignment so the
        # real secret isn't swallowed whole by the outer non-secret key and skipped. (Skip when a
        # marker is present - that case is handled in the secret-key branch above.)
        if ("=" in val or ":" in val) and "[REDACTED" not in val:
            inner = _ASSIGN_BARE.sub(repl_bare, val)
            if inner != val:
                return f"{kq}{key}{kq}{sep}{inner}"
        return m.group(0)

    def repl_shadow(m):
        key, sep, val = m.group("key"), m.group("sep"), m.group("val")
        if not _is_secret_key(key):
            return m.group(0)
        if "[REDACTED" in val:                  # already-redacted: mask only LIVE residue, keep markers
            rebuilt = _value_with_marker_redacted(val, counter)
            return f"{key}{sep}{rebuilt}" if rebuilt is not None else m.group(0)
        if _looks_secret_value(val, standalone=True, explicit=True):
            counter[0] += 1
            return f"{key}{sep}{_mask('secret')}"
        return m.group(0)

    text = _ASSIGN_QUOTED.sub(repl_quoted, text)
    text = _ASSIGN_BARE.sub(repl_bare, text)
    text = _SHADOW_KEY_RE.sub(repl_shadow, text)   # catch prefix-shadowed spaced assignments
    return text


# ---- PII --------------------------------------------------------------------
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(
    r"(?<!\d)(?:\+?\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)"
)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC = re.compile(r"\b(?:\d[ \-]?){13,19}\b")


def _luhn_ok(digits):
    s = 0
    for i, d in enumerate(reversed(digits)):
        d = int(d)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return s % 10 == 0


# DELIBERATE-EVASION HARDENING - the detection copy DROPS every character that renders invisibly, with
# zero advance width, as a non-standard separator, or as a blank spacer glyph, AND canonicalizes
# disguised characters (full-width -> ASCII via NFKC; Latin-look-alike Cyrillic/Greek homoglyphs ->
# ASCII), so a secret SPLIT or DISGUISED with such characters is still caught. Dropped by category:
#   Cf format, Mn/Me/Mc combining marks (diacritics, variation selectors, Indic/Thai signs),
#   Zs/Zl/Zp separators (space, line, paragraph) - EXCEPT the ordinary ASCII space (see below).
# The ASCII space (and TAB/newline, which are Cc) are deliberately NOT folded: they are VISIBLE word/
# line separators, so folding them would redact prose that merely names a token prefix ("sk-ant- is the
# anthropic key prefix ...") and a secret split by a visible separator is visibly malformed.
# _BLANK_GLYPHS adds blank-rendering spacers Unicode does NOT categorize as format/mark/space (the
# category test misses them) - the braille blank cell U+2800 (a REAL screen-reader abuse vector) and the
# Hangul fillers. _HOMOGLYPHS maps confusable Cyrillic/Greek letters to their ASCII skeleton. Folding is
# DETECTION-ONLY: a char is dropped/canonicalized only in the scanned copy; stored text is untouched
# unless a span sits inside a matched secret.
_FOLD_CATEGORIES = frozenset({"Cf", "Mn", "Me", "Mc"})
_SEPARATOR_CATEGORIES = frozenset({"Zs", "Zl", "Zp"})   # space + line + paragraph separators (ASCII space excluded)
_ASCII_SPACE = " "
_BLANK_GLYPHS = frozenset({
    "\u2800",   # BRAILLE PATTERN BLANK - zero-content braille cell (real screen-reader abuse vector)
    "\u115f",   # HANGUL CHOSEONG FILLER
    "\u1160",   # HANGUL JUNGSEONG FILLER
    "\u3164",   # HANGUL FILLER
    "\uffa0",   # HALFWIDTH HANGUL FILLER
})


def _is_folded(ch):
    """True if `ch` is dropped from the detection copy: invisible, zero-advance, an exotic (non-ASCII)
    space, or a known blank-rendering spacer glyph - anything interleaved to split a token WITHOUT
    visibly altering it. The ordinary ASCII space is never folded (it is a real word separator)."""
    if ch == _ASCII_SPACE:
        return False
    cat = unicodedata.category(ch)
    return cat in _FOLD_CATEGORIES or cat in _SEPARATOR_CATEGORIES or ch in _BLANK_GLYPHS


def _normalize_for_detection(text):
    """Return (norm, index_map): the detection copy with folded splitters DROPPED and non-ASCII
    width/compatibility variants NFKC-canonicalized, plus index_map[j] = the ORIGINAL index that
    produced norm[j] (a char may expand to several - each maps back to its source char). ASCII is
    NFKC-invariant, so the common case pays only a category check per char."""
    norm, imap = [], []
    for i, ch in enumerate(text):
        if _is_folded(ch):
            continue
        if ch.isascii():
            canon = ch                       # ASCII is invariant under both homoglyph-fold and NFKC
        elif ch in _HOMOGLYPHS:
            canon = _HOMOGLYPHS[ch]          # Latin-look-alike (Cyrillic/Greek) -> ASCII skeleton
        else:
            canon = unicodedata.normalize("NFKC", ch)   # full-width / compatibility -> canonical
        for c in canon:
            norm.append(c)
            imap.append(i)
    return "".join(norm), imap


# Latin-look-alike homoglyphs (Cyrillic + Greek) -> ASCII skeleton, for DETECTION only, so a token
# disguised with confusables ("SkeXXX" spelled with Cyrillic dze/a) canonicalizes and is caught. Built
# from (codepoint, ascii) pairs so the source carries no ambiguous literal glyphs. NOT exhaustive vs the
# full Unicode confusables table - covers the high-value Latin-target codepoints; exhaustive coverage is
# a documented follow-on (ship confusables.txt as an optional asset). CJK homoglyphs are favored by real
# attackers, so this is first-class, not an edge case.
_HOMOGLYPH_PAIRS = (
    (0x0430, "a"), (0x0435, "e"), (0x043e, "o"), (0x0440, "p"), (0x0441, "c"), (0x0443, "y"),
    (0x0445, "x"), (0x0456, "i"), (0x0458, "j"), (0x0455, "s"), (0x04bb, "h"), (0x0501, "d"),
    (0x0410, "A"), (0x0412, "B"), (0x0415, "E"), (0x041a, "K"), (0x041c, "M"), (0x041d, "H"),
    (0x041e, "O"), (0x0420, "P"), (0x0421, "C"), (0x0422, "T"), (0x0423, "Y"), (0x0425, "X"),
    (0x0405, "S"), (0x0406, "I"), (0x0408, "J"),
    # Cyrillic lowercase confusables for letters that appear in vendor PREFIXES (sk-ant-, xoxb-,
    # glpat-, npm_, ...) - the gap that let a single Cyrillic char in a prefix bypass detection.
    (0x043a, "k"), (0x0442, "t"), (0x0431, "b"), (0x0432, "b"), (0x043c, "m"), (0x043f, "n"),
    (0x0433, "r"), (0x04cf, "l"), (0x051d, "w"),
    (0x03bf, "o"), (0x03bd, "v"), (0x03b1, "a"), (0x03c1, "p"),
    (0x0391, "A"), (0x0392, "B"), (0x0395, "E"), (0x0396, "Z"), (0x0397, "H"), (0x0399, "I"),
    (0x039a, "K"), (0x039c, "M"), (0x039d, "N"), (0x039f, "O"), (0x03a1, "P"), (0x03a4, "T"),
    (0x03a5, "Y"), (0x03a7, "X"),
    # Greek lowercase confusables (+ uppercase lunate sigma) rounding out the prefix alphabet
    (0x03ba, "k"), (0x03c4, "t"), (0x03b2, "b"), (0x03b5, "e"), (0x03b9, "i"), (0x03b7, "n"),
    (0x03c5, "u"), (0x03c9, "w"), (0x03c7, "x"), (0x03b3, "y"), (0x03b6, "z"), (0x03bc, "m"),
    (0x03f2, "c"), (0x03f9, "C"),
)
_HOMOGLYPHS = {chr(_cp): _ascii for _cp, _ascii in _HOMOGLYPH_PAIRS}

# The fold pass runs the token patterns WITHOUT their _NB left-boundary guard. That guard avoids
# false-positives on genuine code identifiers, which never contain folded/disguised chars; a token that
# only became contiguous BECAUSE a folded/disguised char was removed is inherently an evasion and must
# not inherit the same-alnum-glue exemption (glue + a fold char inside the literal prefix was the one
# bypass that survived base-tier). The `text[o0:o1] != norm[fs:fe]` guard still fires ONLY on spans that were
# actually folded/canonicalized, so dropping _NB cannot redact a clean glued identifier.
_NB_PREFIX = "(?<![A-Za-z0-9])"
_FOLD_AWARE = [("private_key", _PEM_BLOCK)]
for _label, _pat in _TOKEN_PATTERNS:
    _src = _pat.pattern
    _FOLD_AWARE.append((_label, re.compile(_src[len(_NB_PREFIX):] if _src.startswith(_NB_PREFIX) else _src)))


def _redact_zero_width_evasions(text, counter):
    """Catch secrets that fixed-shape regexes miss because invisible / zero-advance / blank-glyph
    characters were interleaved inside the token (e.g. 'sk-ant-<U+200B>AAAA...', 'sk-ant-<U+2800>AAAA...')
    OR the token was disguised with full-width / compatibility / homoglyph variants. Detect on the
    normalized copy, but redact ONLY spans that ACTUALLY differ from the original (were folded or
    canonicalized) - so raw secrets (left to the normal passes) and legitimate use OUTSIDE a secret
    (emoji ZWJ, Indic/Arabic joiners, NFD accents, real CJK/Hangul/braille/Cyrillic text) are untouched.
    i18n-safe by construction: the change applies only within a detected secret span.

    Precision/recall stance (deliberate, favoring recall): a folded span cannot be distinguished from an
    accidental copy-paste zero-width/NBSP landing beside a documentation EXAMPLE key, so a split
    well-known placeholder (e.g. AKIA<zwsp>IOSFODNN7EXAMPLE) IS redacted. Masking a doc example is
    harmless; missing a real disguised secret is not - recall wins, consistent with the raw pass already
    redacting a bare 'sk-ant-xxxx...' placeholder shape."""
    norm, imap = _normalize_for_detection(text)
    if norm == text:
        return text                          # nothing folded or normalized -> no hidden secret possible
    spans = []
    for label, pat in _FOLD_AWARE:
        for m in pat.finditer(norm):
            fs, fe = m.start(), m.end()
            if fe == fs:
                continue
            o0, o1 = imap[fs], imap[fe - 1] + 1
            if text[o0:o1] != norm[fs:fe]:   # this span was actually folded/normalized (a hidden secret)
                spans.append((o0, o1, label))
    if not spans:
        return text
    spans.sort(key=lambda s: (s[0], -s[1]))   # at a shared start, the LONGEST span leads (its label wins)
    # merge overlapping/adjacent spans into union ranges so a partial (non-nested) overlap can never
    # leave a tail unredacted - redact the full union with a single marker (the leading span's label).
    merged = []
    for o0, o1, label in spans:
        if merged and o0 <= merged[-1][1]:
            p0, p1, plabel = merged[-1]
            merged[-1] = (p0, max(p1, o1), plabel)
        else:
            merged.append((o0, o1, label))
    out, cur = [], 0
    for o0, o1, label in merged:
        out.append(text[cur:o0])
        out.append(_mask(label))
        counter[0] += 1
        cur = o1
    out.append(text[cur:])
    return "".join(out)


def scrub(text, *, scrub_pii=True):
    """Return (clean_text, n_redactions). Secrets always; PII when scrub_pii."""
    if not text:
        return text, 0
    counter = [0]

    def _pem_repl(m):
        counter[0] += 1
        return _mask("private_key")

    text = _PEM_BLOCK.sub(_pem_repl, text)
    # close the invisible-format-char (zero-width) token-splitting evasion before the contiguous pass
    text = _redact_zero_width_evasions(text, counter)

    for label, pat in _TOKEN_PATTERNS:
        def _repl(m, _label=label):
            counter[0] += 1
            return _mask(_label)

        text = pat.sub(_repl, text)

    def _sig_repl(m):
        counter[0] += 1
        return m.group(1) + "[REDACTED:signature]"

    text = _SIGNED_URL_PARAM.sub(_sig_repl, text)

    def _auth_repl(m):
        val = m.group("val")
        if not _auth_header_value_is_secret(val):
            return m.group(0)
        counter[0] += 1
        return m.group(0)[: m.start("val") - m.start(0)] + "[REDACTED:auth_header]"

    text = _AUTH_HEADER.sub(_auth_repl, text)

    text = _redact_assignments(text, counter)
    text = _redact_marker_glue(text, counter)   # secret glued to a marker via a value-terminator char

    if scrub_pii:
        def _email_repl(m):
            counter[0] += 1
            return "[EMAIL]"

        text = _EMAIL.sub(_email_repl, text)

        def _ssn_repl(m):
            counter[0] += 1
            return "[SSN]"

        text = _SSN.sub(_ssn_repl, text)

        def _cc_repl(m):
            raw = m.group(0)
            digits = re.sub(r"\D", "", raw)
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                counter[0] += 1
                return "[CARD]"
            return raw

        text = _CC.sub(_cc_repl, text)

        def _phone_repl(m):
            counter[0] += 1
            return "[PHONE]"

        text = _PHONE.sub(_phone_repl, text)

    return text, counter[0]


def looks_clean(text):
    """Cheap probe: True if no secret pattern fires (ignores PII)."""
    _, n = scrub(text, scrub_pii=False)
    return n == 0



class ScrubGateError(RuntimeError):
    """The seam scrub gate could not scrub what it was handed, so the write that called it does
    not happen: a gate that cannot run is a refusal, never a pass-through. Until October 2026 an
    enrichment that could not be rebuilt was returned unscrubbed, and label keys and non-string
    label values were never scrubbed at all (REVIEW-2026-10.md, pile one, class (a), scrub.py
    690-691 and 704-707)."""


def _scrub_enrichment(enr, scrub_pii):
    """Return a copy of an enrichment record with its text fields (summary/tags/keywords/
    search_queries) scrubbed. Duck-typed via dataclasses.replace so the scrubber needs no hard
    import of the record type. A record that cannot be rebuilt with its scrubbed fields (not a
    dataclass, a missing field) RAISES ScrubGateError and the caller's write does not happen."""
    import dataclasses
    def _s(x):
        return scrub(x, scrub_pii=scrub_pii)[0] if isinstance(x, str) else x
    def _st(t):
        return tuple(_s(x) for x in (t or ()))
    try:
        return dataclasses.replace(
            enr,
            summary=_s(getattr(enr, "summary", "")),
            tags=_st(getattr(enr, "tags", ())),
            keywords=_st(getattr(enr, "keywords", ())),
            search_queries=_st(getattr(enr, "search_queries", ())),
        )
    except Exception as exc:
        raise ScrubGateError("enrichment of type %s could not be scrubbed (%s); the write did not "
                             "happen" % (type(enr).__name__, type(exc).__name__)) from exc


def _scrub_labels(labels, scrub_pii):
    """Scrub every label KEY and VALUE, or refuse. Keys must be strings and are scrubbed like
    values; string values are scrubbed; a bool or None passes; an int or float is scrubbed in its
    text form and, if anything was redacted, the marker text lands in its place (a card or phone
    number stored as a number is still a number on disk otherwise); any other value type (a list,
    a dict, a set) is refused, because labels are scalar metadata and a container cannot be
    scrubbed field by field here. Two keys that collide after scrubbing with different values are
    refused rather than silently merged."""
    cleaned = {}
    for k, v in labels.items():
        if not isinstance(k, str):
            raise ScrubGateError("label key of type %s cannot be scrubbed; the write did not happen"
                                 % type(k).__name__)
        ck, _ = scrub(k, scrub_pii=scrub_pii)
        if isinstance(v, str):
            v, _ = scrub(v, scrub_pii=scrub_pii)
        elif isinstance(v, bool) or v is None:
            pass
        elif isinstance(v, (int, float)):
            sv, n = scrub(str(v), scrub_pii=scrub_pii)
            if n:
                v = sv
        else:
            raise ScrubGateError("label value of type %s cannot be scrubbed (labels are scalars); "
                                 "the write did not happen" % type(v).__name__)
        if ck in cleaned and cleaned[ck] != v:
            raise ScrubGateError("two label keys collide after scrubbing with different values; "
                                 "the write did not happen")
        cleaned[ck] = v
    return cleaned


def scrub_write(content=None, labels=None, enrichment=None, *, scrub_pii=True):
    """Seam-level write gate: redact secrets (and PII when scrub_pii) from content, every label
    KEY and VALUE, and the enrichment's text fields before storage. Every StoragePort write path
    (store/modify/move) crosses this, so nothing -- the engine API, the Context facade, or a raw
    backend -- can land a secret/PII on disk. Fails CLOSED: anything this gate cannot scrub raises
    ScrubGateError and the write does not happen. Idempotent: an already-scrubbed caller (the
    facade) pays only a no-op re-scan. Returns (content, labels, enrichment)."""
    if content is not None:
        content, _ = scrub(content, scrub_pii=scrub_pii)
    if labels:
        labels = _scrub_labels(labels, scrub_pii)
    if enrichment is not None:
        enrichment = _scrub_enrichment(enrichment, scrub_pii)
    return content, labels, enrichment


if __name__ == "__main__":
    import sys
    data = sys.stdin.read()
    clean, n = scrub(data)
    sys.stderr.write(f"[scrub] {n} redaction(s)\n")
    sys.stdout.write(clean)
