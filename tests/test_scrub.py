#!/usr/bin/env python3
"""Tests for the Parsica Spirit scrubber. Run: python test_scrub.py

Includes regression cases for tricky secret shapes."""
from parsica_spirit.scrub import scrub as _scrub

REDACT = [
    # --- known token shapes ---
    ("anthropic key", "key=sk-ant-api03-Abc123Def456Ghi789Jklmno", "sk-ant-api03-Abc123"),
    ("openai key", "use sk-proj-abcdefghij1234567890ABCDEF here", "sk-proj-abcdefghij"),
    ("openai classic", "token sk-abcdefghij1234567890ABCDEFGH", "sk-abcdefghij1234567890"),
    ("aws access key", "AKIAIOSFODNN7EXAMPLE in config", "AKIAIOSFODNN7EXAMPLE"),
    ("gcp api key", "AIzaSyA1234567890abcdefghijklmnopqrstuv done", "AIzaSyA1234567890abcdef"),
    ("github pat fine", "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz123456", "github_pat_11ABCDEFG0"),
    ("github token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", "ghp_abcdefghijklmnop"),
    ("slack token", "xoxb-123456789012-abcdefghijklmnop", "xoxb-123456789012-abcd"),
    ("slack app token", "xapp-1-A01234ABCDE-1234567890123-abcdef0123456789", "xapp-1-A01234ABCDE"),
    ("stripe live", "sk_live_abcdefghijklmnop1234567890", "sk_live_abcdefghijklmnop"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEFghiJKL", "eyJhbGciOiJIUzI1NiJ9.eyJ"),
    ("slack webhook", "https://hooks.slack.com/services/T000/B000/XXXXabcdEFGH", "/services/T000/B000"),
    ("discord webhook", "https://discord.com/api/webhooks/123456789/abcDEF_ghiJKL-mno", "/webhooks/123456789/abc"),
    ("bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123", "abcdefghijklmnop"),
    ("pem block", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\nDEF==\n-----END RSA PRIVATE KEY-----", "MIIabc"),
    # --- vendor tokens ---
    ("npm token", "export NPM_TOKEN=npm_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", "npm_aBcDeFgHiJkL"),
    ("hf token", "hf_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789Ab", "hf_aBcDeFgHiJkL"),
    ("pypi token", "pypi-AgEIcHlwaS5vcmcabcdefghijklmnop", "pypi-AgEIcHlwaS5vcmc"),
    ("sendgrid", "SG.aBcDeFgHiJkLmNoPqRsTuV.aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdefg", "SG.aBcDeFgHiJkL"),
    ("digitalocean", "dop_v1_" + "a1b2c3d4" * 8, "dop_v1_a1b2c3d4"),
    # --- assignment recall ---
    ("password assign", 'password = "hunter2hunter2"', "hunter2hunter2"),
    ("api_key assign", "OPENAI_API_KEY=abcdef1234567890xyz", "abcdef1234567890xyz"),
    ("client secret", "client_secret: 9f8e7d6c5b4a39281706", "9f8e7d6c5b4a39281706"),
    ("access_token opaque", "access_token=7f3b9a2c4e6d8f0a1b3c5d7e9f1a3b5c7d9e1f3a", "7f3b9a2c4e6d8f0a"),
    ("api_token export", "export API_TOKEN=8f3a2b1c9d8e7f6a5b4c3d2e1f0a9b8c", "8f3a2b1c9d8e7f6a"),
    ("bot_token colon", "BOT_TOKEN: deadbeefcafebabe0123456789abcdef", "deadbeefcafebabe0123"),
    ("discord_token assign", "DISCORD_TOKEN=deadbeefcafebabe0123456789abcdef0011", "deadbeefcafebabe0123"),
    ("azure accountkey", "DefaultEndpointsProtocol=https;AccountName=mystore;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUz/K1SZ==;EndpointSuffix=core.windows.net", "Eby8vdM02xNOcqFlqUwJ"),
    ("json apikey", '{"apiKey": "7f3a9c2b1e8d4f6a0b5c9d2e1f8a3b7c"}', "7f3a9c2b1e8d4f6a"),
    ("json client_secret", '"client_secret": "GOCSPX-aBcDeFgHiJkLmNoPqRsTuVwX"', "GOCSPX-aBcDeFgHiJkL"),
    ("json password", '"password": "S3cretP@sswordValue"', "S3cretP@sswordValue"),
    # --- connection strings ---
    ("redis url empty user", "redis://:topsecretpw1@10.0.0.1:6379", "topsecretpw1"),
    ("postgres url", "postgres://admin:supersecret123@dbhost:5432/mydb", "supersecret123"),
    ("amqp url", "amqp://guest:guestpass123@rabbit:5672", "guestpass123"),
    ("redis url assign", "REDIS_URL=redis://:topsecretpw1@10.0.0.1:6379", "topsecretpw1"),
    ("https basic auth", "git clone https://user:supersecret@example.com/r.git", "user:supersecret@"),
    # --- auth headers ---
    ("auth basic", "Authorization: Basic dXNlcjpzM2NyZXRwYXNzd29yZGxvbmdlbm91Z2g=", "dXNlcjpzM2NyZXRw"),
    ("auth raw", "Authorization: 9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b", "9c8b7a6f5e4d3c2b"),
    ("auth token scheme", "Authorization: Token 9c8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f", "9c8b7a6f5e4d3c2b"),
    # --- structured credential formats ---
    ("stripe test key", "sk_test_abcdefghijklmnop1234567890", "abcdefghijklmnop1234"),
    ("stripe webhook secret", "whsec_abcdefghijklmnop1234567890", "abcdefghijklmnop1234"),
    ("gitlab pat", "glpat-1234567890abcdef1234567890", "1234567890abcdef"),
    ("gitlab oauth token", "gloas-abcdef1234567890abcdef1234", "abcdef1234567890"),
    ("aws presigned signature", "https://s3.amazonaws.com/b/k?X-Amz-Signature=abcdef1234567890abcdef1234567890abcd&X-Amz-Expires=3600", "abcdef1234567890abcdef"),
    ("generic url signature", "download at https://cdn.example.com/f?sig=abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnopqrst"),
    # --- bare value followed by trailing text (regression) ---
    ("bare value trailing text", "use password=hunter2Pass99 here", "hunter2Pass99"),
    ("bare value trailing &&", "export DB_SECRET=AbCdEf1234567890 && run", "AbCdEf1234567890"),
    # --- quoted code-shaped / long secret values ---
    ("quoted dotted secret", 'auth_token="v1.MnoPqRsTuVwXyZ012345"', "v1.MnoPqRsTuVwXyZ012345"),
    ("quoted paren secret", 'secret_key="a8F)kQ2zR7wL1pM4"', "a8F)kQ2zR7wL1pM4"),
    ("quoted gocspx dot", '"client_secret": "GOCSPX.aBcDeFgHiJkLmNoP"', "GOCSPX.aBcDeFgHiJkLmNoP"),
    ("quoted long >256", 'api_key="' + "A1" * 150 + '"', "A1" * 150),
    # --- more vendor tokens ---
    ("docker pat cmd", "docker login -u user -p dckr_pat_C8nZ2kQ9rT5wY1uX3vB7mN6pL0aS4dF8", "dckr_pat_C8nZ2kQ9"),
    ("gocspx bare", "GOCSPX-aBcDeFgHiJkLmNoPqRsTuVwX in config", "GOCSPX-aBcDeFgHiJkL"),
    ("glued underscore token", "VAR=_ghp_abcdefghijklmnopqrstuvwxyz0123456789AB", "ghp_abcdefghijklmnop"),
    # --- secret keys ending in _id / _header (Vault, GCP) ---
    ("vault secret_id", "secret_id=6a174c20-f6de-a53c-74d2-6018fcceff64", "6a174c20-f6de-a53c"),
    ("gcp private_key_id", '"private_key_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"', "a1b2c3d4e5f6a7b8"),
    ("apikey_header opaque", "apikey_header: 1234567890abcdef1234567890abcdef", "1234567890abcdef1234"),
    # --- URL/JDBC query-string secrets ---
    ("url query api_key", "https://api.example.com/v1/data?api_key=abcdef1234567890ABCDEF99&page=1", "abcdef1234567890ABCDEF99"),
    ("jdbc query password", "jdbc:postgresql://host/db?user=admin&password=Sup3rS3cretValue123", "Sup3rS3cretValue123"),
    # --- Telegram bot token (standalone + in-URL) ---
    ("telegram bare", "123456789:AAEhBOweik6ad6PsWpJp0pdsQTQ_Q0w8s8M", "AAEhBOweik6ad6PsWpJp"),
    ("telegram in url", "curl https://api.telegram.org/bot123456789:AAEhBOweik6ad6PsWpJp0pdsQTQ_Q0w8s8M/x", "AAEhBOweik6ad6PsWpJp"),
    # --- all-lowercase passphrase under an explicit secret key ---
    ("lowercase db_password", "export DB_PASSWORD=supersecretword", "supersecretword"),
    ("lowercase passphrase", "SSH_KEY_PASSPHRASE=correcthorsebatterystaple", "correcthorsebatterystaple"),
    ("lowercase password colon", "password: opensesameplease", "opensesameplease"),
    ("quoted lowercase passphrase", 'passphrase: "opensesameopensesame"', "opensesameopensesame"),
    ("quoted lowercase password", '"password": "letmeinplease"', "letmeinplease"),
    # --- PGP armored private key ---
    ("pgp private block", "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF8abcDEF0123456789==\n-----END PGP PRIVATE KEY BLOCK-----", "lQOYBF8abcDEF"),
    # --- long grouped opaque secret (config-enum bypass closed) ---
    ("long grouped secret", "api_key=aB1cD-eF2gH-iJ3kL-mN4oP-qR5sT-uV6wX", "iJ3kL-mN4oP"),
    # --- lowercase passphrase under an explicit key WITH trailing prose ---
    ("lowercase passphrase trailing prose", "use password=correcthorsebatterystaple here", "correcthorsebatterystaple"),
    ("lowercase password colon trailing prose", "password: correcthorsebatterystaple here", "correcthorsebatterystaple"),
    # --- lowercase-only Authorization tokens (scheme is the signal) ---
    ("auth bearer lowercase", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnop"),
    ("auth token lowercase", "Authorization: Token abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnop"),
    # --- auth_key / X-Auth-Key now recognized as a secret-bearing key ---
    ("auth_key assign", "auth_key=abcdef1234567890ABCDEF99", "abcdef1234567890"),
    ("x-auth-key header", "X-Auth-Key: 1234567890abcdef1234567890ABCDEF", "1234567890abcdef"),
    # --- prefix-shadowed inner assignment (header/cookie/curl/log) ---
    ("cookie shadowed token", "Cookie: session_token=9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a", "9d8c7b6a5f4e3d2c"),
    ("set-cookie shadowed", "Set-Cookie: session_token=9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a; HttpOnly", "9d8c7b6a5f4e3d2c"),
    ("curl -H shadowed api_key", "curl -H 'X-Custom: api_key=8f14e45fceea167a5a36dedd4bea2543' https://api", "8f14e45fceea167a"),
    ("log-prefix shadowed api_key", "DEBUG: api_key=8f14e45fceea167a5a36dedd4bea2543", "8f14e45fceea167a"),
    # --- all-lowercase opaque value under a *_key secret key ---
    ("lowercase secret_key hex", "secret_key=fadebabefadebabefadebabefadebabe", "fadebabefadebabe"),
    ("lowercase api_key passphrase", "api_key=correcthorsebatterystaplexyz", "correcthorsebattery"),
    ("lowercase auth_key", "auth_key=abcdefghijklmnopqrstuvwx", "abcdefghijklmnop"),
    # --- no-scheme Authorization opaque value (gateway/proxy log) ---
    ("auth no scheme lowercase", "Authorization: secrettokenvaluegoeshere", "secrettokenvaluegoes"),
    # --- SPACED prefix-shadow (whitespace around the inner sep) ---
    ("spaced shadow debug", "DEBUG: api_key = Xy9kQ2mPvR7sLtN4wB8c", "Xy9kQ2mPvR7sLtN4"),
    ("spaced shadow cookie", "Cookie: session_token = Xy9kQ2mPvR7sLtN4wB8c", "Xy9kQ2mPvR7sLtN4"),
    ("spaced shadow curl", "curl -H 'X: api_key = Xy9kQ2mPvR7sLtN4wB8c'", "Xy9kQ2mPvR7sLtN4"),
    # --- Ruby/Perl => and Go/Pascal := assignment operators ---
    ("hash-rocket api_key", '"api_key" => "AbCdEf0123456789GhIjKlMnOpQr"', "AbCdEf0123456789"),
    ("hash-rocket password", 'password => "AbCdEf0123456789GhIjKlMnOpQr"', "AbCdEf0123456789"),
    ("walrus client_secret", "client_secret := AbCdEf0123456789GhIjKlMnOpQr", "AbCdEf0123456789"),
    # --- compound-assignment operators store a secret literal ---
    ("memoized ||= secret", 'api_key ||= "aB3xK9mPqR7sT2vWxYz1"', "aB3xK9mPqR7sT2vWxYz1"),
    ("append += secret", 'password += "MyRealPassw0rdValue"', "MyRealPassw0rdValue"),
    ("nullish ??= secret", 'api_key ??= "aB3xK9mPqR7sT2vWxYz1"', "aB3xK9mPqR7sT2vWxYz1"),
    # --- a LIVE secret glued before an in-place marker is still redacted ---
    ("live secret before marker", "api_key=aB3xK9mPqR7sT2vWxYz1[REDACTED:secret]", "aB3xK9mPqR7sT2vWxYz1"),
    ("live secret before marker quoted", 'password = "MyRealPassw0rdValue[REDACTED:secret]"', "MyRealPassw0rdValue"),
    # --- a live secret glued AFTER a marker (concatenated secrets) ---
    ("after-marker aws secret", "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE.wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY99", "wJalrXUtnFEMIK7"),
    ("after-marker quoted aws", 'aws_secret_access_key="AKIAIOSFODNN7EXAMPLE.wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY99"', "wJalrXUtnFEMIK7"),
    ("after-marker glued plus", "api_key=AKIAIOSFODNN7EXAMPLE+aB3xK9mNp2qR5tY7wZ1cQ8", "aB3xK9mNp2qR5tY7"),
    # --- live secret glued to a marker via a value-TERMINATOR ---
    ("marker comma-glued secret", "password=AKIAIOSFODNN7EXAMPLE,wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMIK7"),
    ("marker semicolon-glued secret", "password=AKIAIOSFODNN7EXAMPLE;wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMIK7"),
    ("marker amp-glued secret", "api_key=AKIAIOSFODNN7EXAMPLE&aB3xK9mNp2qR5tY7wZ1cQ8", "aB3xK9mNp2qR5tY7"),
    ("marker close-paren-glued secret", "password=[REDACTED:a])wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMIK7"),
    # --- invisible zero-width evasion (Cf chars interleaved inside a token to split it); explicit
    #     \u escapes so an editor/lint pass can't silently strip the char and void the regression ---
    ("zw anthropic bare", "sk-ant-\u200bAAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAAAA"),
    ("zw anthropic assign", "api_key=sk-ant-\u200bBBBBBBBBBBBBBBBBBBBBBBBB", "BBBBBBBBBBBBBBBBBBBBBBBB"),
    ("zw jwt", "eyJ\u200babcdefgh.abcdefgh.abcdefghijklmnop", "abcdefghijklmnop"),
    ("zw github token", "ghp_\u200b" + "c" * 40, "c" * 40),
    ("zw word-joiner sk-ant", "sk-ant-\u2060DDDDDDDDDDDDDDDDDDDDDDDD", "DDDDDDDDDDDDDDDDDDDDDDDD"),
    ("zw soft-hyphen sk-ant", "sk-ant-\u00adEEEEEEEEEEEEEEEEEEEEEEEE", "EEEEEEEEEEEEEEEEEEEEEEEE"),
    # combining marks (Mn) and variation selectors are also invisible/zero-advance splitters
    ("mn combining sk-ant", "sk-ant-\u0301AAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAAAA"),
    ("mn combining aws", "AKIA\u0301IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    ("variation-selector sk-ant", "sk-ant-\ufe0fBBBBBBBBBBBBBBBBBBBBBBBB", "BBBBBBBBBBBBBBBBBBBBBBBB"),
    # exotic / non-breaking spaces (Zs, not the ASCII space) and spacing marks (Mc) also split tokens
    ("nbsp split sk-ant", "sk-ant-\u00a0AAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAAAA"),
    ("narrow-nbsp aws", "AKIA\u202fIOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    ("thin-space ghp", "ghp_\u2009" + "c" * 40, "cccccccccccccccccccc"),
    ("ideographic-space sk-ant", "sk-ant-\u3000BBBBBBBBBBBBBBBBBBBBBBBB", "BBBBBBBBBBBBBBBBBBBBBBBB"),
    ("mc devanagari sk-ant", "sk-ant-\u0903CCCCCCCCCCCCCCCCCCCCCCCC", "CCCCCCCCCCCCCCCCCCCCCCCC"),
    # deliberate blank-glyph spacers (braille blank, Hangul fillers) + full-width/compat disguise
    ("braille-blank sk-ant", "sk-ant-\u2800AAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAAAA"),
    ("hangul-filler sk-ant", "sk-ant-\u3164BBBBBBBBBBBBBBBBBBBBBBBB", "BBBBBBBBBBBBBBBBBBBBBBBB"),
    ("fullwidth sk-ant", "\uff53\uff4b-\uff41\uff4e\uff54-AAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAAAA"),
    ("fullwidth aws", "\uff21\uff2b\uff29\uff21IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    # glue + invisible-char-in-prefix, homoglyph disguise (Cyrillic/Greek), and line/para separators
    ("glued zwsp sk-ant", "docs herefors\u200bk-ant-api03-Ab12Cd34Ef56Gh78Ij90Kl end", "Ab12Cd34Ef56Gh78Ij90Kl"),
    ("glued braille AKIA", "keyAKIA\u2800IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    ("homoglyph cyrillic sk-ant", "key \u0455k-\u0430nt-api03-Ab12Cd34Ef56Gh78Ij90Kl", "Ab12Cd34Ef56Gh78Ij90Kl"),
    ("homoglyph cyrillic aws", "\u0410\u041a\u0406\u0410IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    # homoglyph substitution INSIDE the vendor prefix (k/t/b/n) - a known gap, must not regress
    ("homoglyph cyrillic-k prefix", "s\u043a-ant-api03-Ab12Cd34Ef56Gh78Ij90Kl", "Ab12Cd34Ef56Gh78Ij90Kl"),
    ("homoglyph cyrillic-t prefix", "sk-an\u0442-api03-Ab12Cd34Ef56Gh78Ij90Kl", "Ab12Cd34Ef56Gh78Ij90Kl"),
    ("homoglyph greek-k prefix", "s\u03ba-ant-api03-Ab12Cd34Ef56Gh78Ij90Kl", "Ab12Cd34Ef56Gh78Ij90Kl"),
    ("homoglyph cyrillic-b xoxb", "\u0445\u043e\u0445\u0431-123456789012-abcdefghijklmnop", "abcdefghijklmnop"),
    ("homoglyph cyrillic-t glpat", "glpa\u0442-1234567890abcdef1234567890", "1234567890abcdef"),
    ("line-sep U+2028 sk-ant", "sk-ant-\u2028AAAAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAAAA"),
    ("para-sep U+2029 sk-ant", "sk-ant-\u2029BBBBBBBBBBBBBBBBBBBBBBBB", "BBBBBBBBBBBBBBBBBBBBBBBB"),
]

KEEP = [
    # (description, input, must-still-appear-substring) - MUST NOT be redacted
    # --- the operator's SHA-heavy workflow ---
    ("git sha short", "fixed in commit 4f49f39", "4f49f39"),
    ("git sha full", "HEAD is at d383dc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708", "d383dc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708"),
    ("version", "running version 4.8.0 build 26200", "4.8.0"),
    ("file path", "edited acme_agent/security/effects.py:180", "effects.py:180"),
    ("normal prose", "the password reset flow needs a test", "password reset flow"),
    ("placeholder angle", "set token=<your-token-here>", "<your-token-here>"),
    ("placeholder var", "export TOKEN=$MY_TOKEN", "$MY_TOKEN"),
    ("placeholder stars", "password = ********", "********"),
    ("hex hash", "sha256: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"),
    ("port number", "listening on 127.0.0.1:8080", "127.0.0.1:8080"),
    ("test id", "test_control_auth.py::test_verify", "test_control_auth.py"),
    # --- code that must NOT be gutted ---
    ("private_key code", "self.private_key = load_pem(path)", "load_pem(path)"),
    ("api_key attr", "api_key = settings.API_KEY", "settings.API_KEY"),
    ("access_key call", "self.access_key = compute_cache_key(self.inputs)", "compute_cache_key(self.inputs)"),
    ("private_key_path", "private_key_path = /home/op/.ssh/id_ed25519.pub", "/home/op/.ssh/id_ed25519.pub"),
    ("pwd getcwd", "pwd = os.getcwd()", "os.getcwd()"),
    ("passwd parse", "passwd_entry = parse_etc_passwd_line(line)", "parse_etc_passwd_line(line)"),
    ("secretKey call", "secretKey = generateDeterministicId(seed)", "generateDeterministicId(seed)"),
    ("session_token attr", "session_token = request.session.session_key", "request.session.session_key"),
    ("api_key_name quoted", "api_key_name = 'production-readonly'", "production-readonly"),
    ("apikey_header call", 'apikey_header = request.headers.get("X-Api-Key")', "request.headers.get"),
    ("auth_token_regex", 'auth_token_regex = re.compile(r"[A-Za-z0-9]+")', "re.compile"),
    ("cap_secret_grant call", "cap_secret_grant = mint(holder, scope)", "mint(holder, scope)"),
    ("cache_key ordinary", "cache_key = build_cache_key(req)", "build_cache_key(req)"),
    # --- prose after secret-word labels ---
    ("token prose camel", "token: nextPageToken returned by the paginated list endpoint", "nextPageToken"),
    ("password prose", "password: choose something memorable but unique to this site", "choose something"),
    ("secret prose snake", "secret = the_function_returns_early_on_empty_input", "the_function_returns_early_on_empty_input"),
    ("password pick short", "password: pick a strong one", "pick a strong one"),
    # --- conn-string negatives ---
    ("github https", "https://github.com/org/repo.git", "github.com/org/repo.git"),
    ("ssh scp url", "git@github.com:org/repo.git", "git@github.com"),
    ("localhost url", "http://localhost:3000/path", "localhost:3000/path"),
    # --- auth-header negatives ---
    ("auth prose", "Authorization: Required for this endpoint", "Required for this endpoint"),
    ("auth word", "Authorization: indistinguishable", "indistinguishable"),
    ("bearer prose snake", "Bearer authentication_is_required_here_now", "authentication_is_required"),
    ("auth bearer required prose", "Authorization: Bearer required", "Bearer required"),
    ("auth token prose long", "Authorization: Token requiredforendpoint", "requiredforendpoint"),
    # --- quoted config-enum values under secret keys (precision) ---
    ("quoted cipher enum", '"encryption_key": "AES-256-GCM"', "AES-256-GCM"),
    ("quoted region", 'secret_key: "us-east-1"', "us-east-1"),
    ("quoted version", 'client_secret = "v1.2.3-beta"', "v1.2.3-beta"),
    ("quoted not-configured", 'client_secret: "not-configured-yet"', "not-configured-yet"),
    # --- precision tightening ---
    ("camelcase identifier", "auth_token = AuthTokenProvider", "AuthTokenProvider"),
    ("pascal secret code", "secret = CapabilityGrant", "CapabilityGrant"),
    ("conn host port host", "redis://cache:6379@primary", "cache:6379@primary"),
    ("token_url reference", "token_url: https://auth.example.com/oauth/token", "https://auth.example.com/oauth/token"),
    ("secret_version ref", "secret_version = build-2026-06-27-rc1", "build-2026-06-27-rc1"),
    # --- precision under explicit keys ---
    ("camelcase under token key", "auth_token = AuthTokenProvider", "AuthTokenProvider"),
    ("snake phrase under secret", "secret = the_function_returns_early_on_empty_input", "the_function_returns_early_on_empty_input"),
    ("vault kv path", "vault_secret = secret/data/prod/db", "secret/data/prod/db"),
    ("pgp public block kept", "-----BEGIN PGP PUBLIC KEY BLOCK-----\nmQENBF8abcDEF0123456789==\n-----END PGP PUBLIC KEY BLOCK-----", "mQENBF8abcDEF"),
    ("cipher under secret key", "secret_cipher = TLS_AES_128_GCM_SHA256", "TLS_AES_128_GCM_SHA256"),
    # --- precision boundary on the lowercase-passphrase widening ---
    # an 11-char lowercase word + trailing prose under an explicit key is prose, not a secret
    ("password 11-char word trailing", "password: complicated stuff to recall", "complicated"),
    ("auth scheme short prose", "Authorization: Bearer shortish", "shortish"),
    # --- prose words after a literal password/secret key survive ---
    ("password prose yesterday", "password: yesterday", "yesterday"),
    ("password prose regenerated", "password: regenerated", "regenerated"),
    ("secret prose configuration", "secret: configuration was the issue", "configuration"),
    ("secret prose authentication", "secret: authentication is broken now", "authentication"),
    # --- *_key explicit must not gut a short lowercase config word ---
    ("api_key short config word", "api_key = production", "production"),
    # --- a non-secret prefix + inner non-secret assignment survives ---
    ("log non-secret inner assign", "log: env=prod port=8080", "port=8080"),
    # --- long lowercase snake/hyphen identifier after Authorization: survives ---
    ("auth snake ident", "We pass Authorization: x_internal_request_id downstream.", "x_internal_request_id"),
    ("auth hyphen ident", "The Authorization: middleware-request-validator rejected it.", "middleware-request-validator"),
    # --- a comparison operator (==) is not an assignment - no false redaction ---
    ("comparison == not assign", "if count == 1000000000 then halt", "1000000000"),
    # --- a marker beside a git SHA / long word under a NON-secret key survives ---
    ("marker beside sha non-secret key", 'commit_note = "[REDACTED] 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"', "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"),
    ("marker beside word non-secret key", 'log_line = "value [REDACTED] internationalization"', "internationalization"),
    # --- marker + terminator must NOT absorb a following key=value / enum / snake / path ---
    ("marker semicolon then keyvalue", "conn=[REDACTED:secret];Host=database_primary", "Host=database_primary"),
    ("marker comma then enum", "data=[REDACTED:x],us-east-1", "us-east-1"),
    ("marker comma then snake", "ref=[REDACTED:x],the_function_name_here", "the_function_name_here"),
    ("marker comma then path", "p=[REDACTED:x],/etc/app/config/store.enc", "/etc/app/config/store.enc"),
    # --- invisible format chars OUTSIDE any secret are preserved (i18n / emoji), never stripped:
    #     the zero-width fix folds them for DETECTION only, redacting solely within a real secret ---
    ("emoji zwj family", "family \U0001f468\u200d\U0001f469\u200d\U0001f467 ok", "\U0001f468\u200d\U0001f469"),
    ("farsi zwnj word", "می\u200cشود done", "ی\u200cش"),
    ("nfd accented word", "cafe\u0301 latte", "cafe\u0301 latte"),
    ("emoji variation selector", "wave \u270b\ufe0f done", "\u270b\ufe0f"),
    # the ordinary ASCII space is a word separator, NOT folded: prose naming a token prefix survives
    ("prose names sk-ant prefix", "sk-ant- is the standard anthropic api key prefix used everywhere", "sk-ant- is the standard anthropic api key prefix used everywhere"),
    ("prose names aws prefix", "the AKIA prefix marks an aws access key id in config", "the AKIA prefix marks an aws access key id in config"),
    ("nbsp thousands legit", "we shipped 10\u00a0000 units last quarter", "10\u00a0000"),
    # only the BLANK braille cell folds, not real braille; full-width prose is normalized-for-scan only
    ("real braille letters", "\u2801\u2803\u2809 braille abc", "\u2801\u2803\u2809"),
    ("fullwidth prose", "\uff28\uff45\uff4c\uff4c\uff4f full width hello", "\uff28\uff45\uff4c\uff4c\uff4f"),
    # real Cyrillic / Greek prose (homoglyph map is detection-only + pattern-gated) is preserved
    ("real cyrillic prose", "\u043f\u0440\u0438 hello world", "\u043f\u0440\u0438"),
    ("real greek prose", "\u03b1\u03b2\u03b3 delta math", "\u03b1\u03b2\u03b3"),
]

PII = [
    ("email", "ping me at sam@acme-example.io", "[EMAIL]", "sam@acme-example.io"),
    ("ssn", "ssn 123-45-6789 on file", "[SSN]", "123-45-6789"),
    ("phone", "call +1 415-555-0142 today", "[PHONE]", "415-555-0142"),
]

PII_KEEP = [
    ("date not phone", "the 2026-06-27 release", "2026-06-27"),
    ("version not phone", "v1.2.3.4 shipped", "1.2.3.4"),
]


def run():
    fails = []

    for desc, inp, banned in REDACT:
        out, n = _scrub(inp)
        if banned in out:
            fails.append(f"REDACT[{desc}] secret survived: {banned!r} still in {out!r}")
        elif n == 0:
            fails.append(f"REDACT[{desc}] no redaction counted for {inp!r}")

    for desc, inp, must in KEEP:
        out, n = _scrub(inp, scrub_pii=False)
        if must not in out:
            fails.append(f"KEEP[{desc}] over-redacted: {must!r} gone from {out!r}")

    for desc, inp, token, banned in PII:
        out, n = _scrub(inp, scrub_pii=True)
        if banned in out:
            fails.append(f"PII[{desc}] survived: {banned!r} in {out!r}")
        if token not in out:
            fails.append(f"PII[{desc}] wrong token, got {out!r}")
        out2, _ = _scrub(inp, scrub_pii=False)
        if banned not in out2:
            fails.append(f"PII[{desc}] redacted even with scrub_pii=False: {out2!r}")

    for desc, inp, must in PII_KEEP:
        out, n = _scrub(inp, scrub_pii=True)
        if must not in out:
            fails.append(f"PII_KEEP[{desc}] over-redacted: {must!r} gone from {out!r}")

    # an already-redacted inner value must NOT be re-mangled into
    # '[REDACTED:secret]]' (stray bracket) or double-counted by the bare/shadow pass.
    for desc, inp in (("shadow remangle", "config: api_key = api_token=anotherSecret456"),
                      ("bare remangle", "api_key=prefix_sk-ant-abcdefghij1234567890klmnop_x"),
                      ("live before marker", "api_key=aB3xK9mPqR7sT2vWxYz1[REDACTED:secret]")):
        out, n = _scrub(inp, scrub_pii=False)
        if "]]" in out:
            fails.append(f"MANGLE[{desc}] stray bracket in {out!r}")
        if n != 1:
            fails.append(f"MANGLE[{desc}] expected n=1, got n={n} for {inp!r}")

    total = len(REDACT) + len(KEEP) + len(PII) + len(PII_KEEP)
    if fails:
        print(f"FAIL - {len(fails)}/{total} checks failed:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"OK - all {total} scrub checks passed "
          f"({len(REDACT)} redact, {len(KEEP)} keep, {len(PII)} pii, {len(PII_KEEP)} pii-keep)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(run())


def test_scrub_gauntlet():
    # the scrub gate must hold ALL invariants: every REDACT
    # secret dies, every KEEP survives, PII toggles, no marker double-mangle.
    assert run() == 0, "scrub adversarial failed - see captured stdout for surviving/over-redacted cases"
