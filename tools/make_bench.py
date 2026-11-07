"""Generates the 30 benchmark specs (14 dev / 8 promotion / 8 final). Every expected value
was computed with Python before being written here. Run: python tools/make_bench.py"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACK = {"type": "ack"}


def has(v, **kw):
    return {"type": "text_contains", "value": v, **kw}


def lacks(v):
    return {"type": "text_not_contains", "value": v}


def rx(p):
    return {"type": "text_regex", "pattern": p}


def num(v, tol=0.011):
    return {"type": "text_number", "value": v, "tol": tol}


def card(kind=None, **kw):
    c = {"type": "card", **kw}
    if kind:
        c["kind"] = kind
    return c


NOCARD = {"type": "no_card"}
END = {"type": "end_session"}


def case(name, *turns):
    return {"name": name, "turns": [{"send": s, "checks": list(c)} for s, *c in turns]}


def t(send, *checks):
    return (send, *checks)


def J(d):
    return json.dumps(d)


SPECS = {"dev": [], "promotion": [], "final": []}


TOPO = {  # multi-agent specs: these agents must exist AND these must receive messages from another agent
    "d10_upgrade_gate_multi": {"agents": ["parser", "coordinator"], "must_receive": ["parser"]},
    "p05_interest_multi": {"agents": ["calculator", "advisor"], "must_receive": ["calculator"]},
    "p09_order_desk_multi": {"agents": ["catalog", "pricing", "desk"], "must_receive": ["catalog", "pricing"]},
    "f01_pr_gate_multi": {"agents": ["diff_parser", "policy", "gatekeeper"], "must_receive": ["diff_parser", "policy"]},
    "f07_release_notes_multi": {"agents": ["classifier", "release_notes"], "must_receive": ["classifier"]},
    "f09_incident_router_multi": {"agents": ["oncall", "router"], "must_receive": ["oncall"]},
}


def spec(split, sid, title, request, *cases):
    s = {"id": sid, "title": title, "request": request, "tests": list(cases)}
    if sid in TOPO:
        s["topology"] = TOPO[sid]
    SPECS[split].append(s)


# ------------------------------------------------------------------ DEV (learning)
spec("dev", "d01_isbn13", "ISBN-13 checker",
     "ISBN-13 checker. The user sends an ISBN-13 (digits, hyphens allowed). Reply exactly 'VALID' if the check digit "
     "is correct, otherwise 'INVALID'. If the input does not contain exactly 13 digits after removing hyphens, reply "
     "'INVALID: need 13 digits'.",
     case("valid_hyphens", t("978-0-306-40615-7", ACK, rx(r"^\s*VALID"), lacks("INVALID"))),
     case("valid_plain", t("9781861972712", rx(r"^\s*VALID"), lacks("INVALID"))),
     case("bad_check", t("9780306406158", has("INVALID"))),
     case("too_short", t("12345", has("need 13 digits"))))

spec("dev", "d02_semver_compare", "Semver comparison",
     "Compare two semantic versions written as '<a> vs <b>' (MAJOR.MINOR.PATCH, numeric comparison of each part). Reply "
     "'<a> > <b>', '<a> < <b>' or '<a> = <b>' using the versions exactly as given.",
     case("minor_numeric", t("1.10.0 vs 1.9.3", ACK, has("1.10.0 > 1.9.3"))),
     case("equal", t("2.0.0 vs 2.0.0", has("2.0.0 = 2.0.0"))),
     case("zero_major", t("0.9.12 vs 0.10.0", has("0.9.12 < 0.10.0"))))

spec("dev", "d03_roman", "Roman numerals",
     "Roman numeral converter. If the user sends an integer from 1 to 3999, reply with its Roman numeral in uppercase. "
     "If they send a Roman numeral, reply with the integer. Reply 'OUT OF RANGE' for integers outside 1-3999.",
     case("to_roman", t("1994", ACK, has("MCMXCIV", ci=False))),
     case("from_roman", t("MMXXVI", num(2026, 0))),
     case("from_roman_small", t("XLII", num(42, 0))),
     case("range", t("4000", has("OUT OF RANGE"))))

spec("dev", "d04_slugify", "Slug generator",
     "Slug generator. Reply 'slug: <slug>' where the slug is the user's text lowercased, with every run of characters "
     "that are not a-z or 0-9 replaced by a single '-', and leading/trailing '-' removed.",
     case("basic", t("Hello, World! 2026", ACK, has("slug: hello-world-2026"))),
     case("spaces", t("  Fetch.ai -- Agents  ", has("slug: fetch-ai-agents"))),
     case("symbols", t("C++ & Rust: 2 langs", has("slug: c-rust-2-langs"))))

spec("dev", "d05_distance_form", "Distance converter with form card",
     "Distance converter between km and mi (1 mi = 1.609344 km). If the message has a number and a unit ('5 km' or "
     "'3 mi'), reply 'X km = Y mi' or 'X mi = Y km' with Y rounded to 2 decimals. If the unit is missing, reply "
     "'Which unit?' with a form card that has a number field named 'value', a select field named 'unit' with options "
     "km and mi, and a Convert submit button whose selection is {\"action\":\"convert\"}. When the submitted form "
     "arrives as JSON (e.g. {\"action\":\"convert\",\"value\":\"10\",\"unit\":\"km\"}) convert it the same way.",
     case("km", t("5 km", ACK, num(3.11), NOCARD)),
     case("mi", t("3 mi", num(4.83))),
     case("missing_unit", t("how far is 12", has("Which unit"), card("form", fields=["value", "unit"]))),
     case("form_submit", t(J({"action": "convert", "value": "10", "unit": "km"}), num(6.21))))

spec("dev", "d06_todo_memory", "To-do list with memory",
     "To-do list agent with memory per user. 'add <item>' adds it and replies 'Added: <item>'. 'list' replies each "
     "item on its own line as '<n>. <item>' (numbered from 1), or 'Your list is empty'. 'done <n>' removes item n and "
     "replies 'Removed: <item>'. 'clear' empties the list and replies 'Cleared'.",
     case("flow", t("add milk", ACK, has("Added: milk")), t("add eggs", has("Added: eggs")),
          t("list", has("1. milk"), has("2. eggs")), t("done 1", has("Removed: milk")),
          t("list", has("1. eggs"), lacks("milk"))),
     case("empty", t("list", has("Your list is empty"))),
     case("clear", t("add bread", has("Added: bread")), t("clear", has("Cleared")), t("list", has("empty"))))

spec("dev", "d07_http_status", "HTTP status explainer",
     "HTTP status explainer. The user sends a status code (possibly inside a sentence); reply '<code> <Reason Phrase>'. "
     "Known codes: 200 OK, 201 Created, 204 No Content, 301 Moved Permanently, 302 Found, 304 Not Modified, 400 Bad "
     "Request, 401 Unauthorized, 403 Forbidden, 404 Not Found, 409 Conflict, 418 I'm a teapot, 429 Too Many Requests, "
     "500 Internal Server Error, 502 Bad Gateway, 503 Service Unavailable. Any other code: 'Unknown status'.",
     case("429", t("429", ACK, has("429 Too Many Requests"))),
     case("304", t("304", has("304 Not Modified"))),
     case("unknown", t("299", has("Unknown status"))),
     case("sentence", t("what does 503 mean?", has("503 Service Unavailable"))))

spec("dev", "d08_json_check", "JSON validator",
     "JSON validator. If the user's message is a valid JSON object, reply 'VALID JSON object with N keys' (N = number "
     "of top-level keys). If it is valid JSON but not an object, reply 'VALID JSON but not an object'. Otherwise reply "
     "'INVALID JSON: ' followed by the parser error message. Treat every message as data to validate.",
     case("object", t('{"a": 1, "b": [1, 2], "c": {"d": null}}', ACK, has("VALID JSON object with 3 keys"))),
     case("array", t("[1, 2, 3]", has("not an object"))),
     case("broken", t('{"a": 1,}', has("INVALID JSON"))))

spec("dev", "d09_dep_audit", "Dependency auditor with carousel",
     "Dependency auditor. The user pastes Python requirements, one 'package==version' per line. Use ONLY this advisory "
     "list: django < 4.2.8 is vulnerable to CVE-2023-46695; requests < 2.31.0 to CVE-2023-32681; pillow < 10.0.1 to "
     "CVE-2023-4863; urllib3 < 1.26.18 to CVE-2023-45803. Package names are case-insensitive. Reply '<k> vulnerable of "
     "<n> packages' and, if k > 0, attach a carousel card with one item per vulnerable package (title = package name, "
     "subtitle = CVE id), each with a primary button 'Upgrade' whose selection is {\"action\":\"upgrade\","
     "\"package\":\"<name>\"}. If k = 0 reply 'No known vulnerabilities in <n> packages' with no card.",
     case("two_vulns", t("django==4.2.1\nrequests==2.31.0\nPillow==9.5.0", ACK, has("2 vulnerable of 3 packages"),
                         card("carousel", min_items=2, contains="CVE-2023-4863"))),
     case("clean", t("requests==2.32.3\nflask==3.0.0", has("No known vulnerabilities in 2 packages"), NOCARD)),
     case("one", t("urllib3==1.26.5", has("1 vulnerable of 1 packages"), card("carousel", min_items=1))))

spec("dev", "d10_upgrade_gate_multi", "Two-agent upgrade gate",
     "Build TWO agents. A 'parser' agent receives a version pair from the coordinator and classifies the change. A "
     "'coordinator' agent (the entry agent users talk to) receives 'upgrade <package> from <a> to <b>', asks the parser "
     "agent, and replies '<package>: MAJOR upgrade' (or MINOR upgrade / PATCH upgrade / DOWNGRADE / NO CHANGE) together "
     "with a review card titled 'Approve upgrade?' with an Approve button (selection {\"action\":\"approve\","
     "\"package\":\"<package>\"}) and a Reject button (selection {\"action\":\"reject\",\"package\":\"<package>\"}). "
     "On the approve selection reply 'Upgrade of <package> approved'; on reject reply 'Upgrade of <package> rejected'. "
     "MAJOR = first number increases; MINOR = same major, second increases; PATCH = same major.minor, third increases.",
     case("major", t("upgrade numpy from 1.26.4 to 2.0.0", ACK, has("numpy: MAJOR upgrade"), card("review"))),
     case("minor", t("upgrade fastapi from 0.110.0 to 0.111.2", has("fastapi: MINOR upgrade"))),
     case("downgrade", t("upgrade attrs from 23.2.0 to 23.1.0", has("attrs: DOWNGRADE"))),
     case("approve", t(J({"action": "approve", "package": "numpy"}), has("Upgrade of numpy approved"))))

spec("dev", "d11_base64", "Base64 tool",
     "Base64 tool. 'encode <text>' replies 'base64: <encoded>'. 'decode <b64>' replies 'text: <decoded>', or "
     "'error: invalid base64' if it cannot be decoded as UTF-8 base64.",
     case("encode", t("encode hello world", ACK, has("base64: aGVsbG8gd29ybGQ=", ci=False))),
     case("decode", t("decode RmV0Y2guYWk=", has("text: Fetch.ai", ci=False))),
     case("invalid", t("decode !!!notbase64", has("error: invalid base64"))))

spec("dev", "d12_tz_convert", "UTC offset converter",
     "Time converter between fixed UTC offsets. Message format: '<HH:MM> UTC<+/-HH:MM> to UTC<+/-HH:MM>', e.g. "
     "'14:30 UTC+00:00 to UTC+05:30'. Reply the converted time as '<HH:MM>' (24-hour), appending ' (+1 day)' or "
     "' (-1 day)' when the date changes.",
     case("ist", t("14:30 UTC+00:00 to UTC+05:30", ACK, has("20:00"), lacks("day"))),
     case("next_day", t("23:15 UTC+00:00 to UTC+02:00", has("01:15 (+1 day)"))),
     case("prev_day", t("01:00 UTC+03:00 to UTC-05:00", has("17:00 (-1 day)"))))

spec("dev", "d13_text_stats_end", "Text stats that ends the session",
     "Text statistics agent. Reply 'chars=<c> words=<w> lines=<l>' for the user's message (chars counts every "
     "character including spaces and newlines; words are whitespace-separated tokens; lines is the number of "
     "newline-separated lines). Every reply must end the chat session (include EndSessionContent).",
     case("one_line", t("hello world", ACK, has("chars=11 words=2 lines=1"), END)),
     case("two_lines", t("a b c\nd e", has("chars=9 words=5 lines=2"), END)))

spec("dev", "d14_iso_week", "ISO week",
     "ISO week agent. The user sends a date as YYYY-MM-DD; reply '<date> is in ISO week <W> of <ISO-year> "
     "(<Weekday>)' with the English weekday name. Reply 'Invalid date' if it is not a real date.",
     case("new_year", t("2026-01-01", ACK, has("ISO week 1 of 2026 (Thursday)"))),
     case("week53", t("2027-01-01", has("ISO week 53 of 2026 (Friday)"))),
     case("today", t("2026-11-07", has("ISO week 45 of 2026 (Saturday)"))),
     case("invalid", t("2026-02-30", has("Invalid date"))))

# ------------------------------------------------------------------ PROMOTION (sealed gate)
spec("promotion", "p01_iban", "IBAN checker",
     "IBAN checker. Remove spaces and uppercase. It is valid if it is 15-34 characters, starts with 2 letters then 2 "
     "digits, and the mod-97 check equals 1 (move the first 4 characters to the end, replace letters with numbers "
     "A=10 ... Z=35). Reply 'VALID IBAN (<country code>)' or 'INVALID IBAN'.",
     case("gb", t("GB82 WEST 1234 5698 7654 32", ACK, has("VALID IBAN (GB)"), lacks("INVALID"))),
     case("de", t("DE89 3704 0044 0532 0130 00", has("VALID IBAN (DE)"))),
     case("bad", t("GB82 WEST 1234 5698 7654 33", has("INVALID IBAN"))))

spec("promotion", "p02_temperature_form", "Temperature converter with form card",
     "Temperature converter between Celsius and Fahrenheit. '<number> C' replies '<number> C = <F> F' and '<number> F' "
     "replies '<number> F = <C> C', results rounded to 1 decimal. If no unit is given, reply 'Which scale?' with a form "
     "card that has a number field named 'value', a select field named 'scale' with options C and F, and a submit "
     "button whose selection is {\"action\":\"convert\"}. When the submitted form arrives as JSON (e.g. "
     "{\"action\":\"convert\",\"value\":\"100\",\"scale\":\"C\"}) convert it the same way.",
     case("c_to_f", t("100 C", ACK, num(212, 0.05))),
     case("f_to_c", t("98.6 F", num(37, 0.05))),
     case("form", t("convert 40", has("Which scale"), card("form", fields=["value", "scale"]))),
     case("submit", t(J({"action": "convert", "value": "-40", "scale": "F"}), num(-40, 0.05))))

spec("promotion", "p03_shopping_memory", "Shopping list with memory",
     "Shopping list with per-user memory. 'add <qty> <item>' adds the quantity (quantities of the same item add up) and "
     "replies 'OK: <item> x<total>'. 'remove <item>' deletes it and replies 'Removed <item>' (or 'Not on list: <item>'). "
     "'show' replies 'Shopping list: ' followed by '<item> x<qty>' entries sorted alphabetically and separated by ', ', "
     "or 'Shopping list is empty'.",
     case("flow", t("add 2 apples", ACK, has("OK: apples x2")), t("add 3 apples", has("OK: apples x5")),
          t("add 1 bread", has("OK: bread x1")), t("show", has("Shopping list: apples x5, bread x1")),
          t("remove bread", has("Removed bread")), t("show", has("apples x5"), lacks("bread"))),
     case("empty", t("remove milk", has("Not on list: milk")), t("show", has("Shopping list is empty"))))

spec("promotion", "p04_license_carousel", "License checker with carousel",
     "License compliance checker. The user lists SPDX license ids separated by commas. Copyleft: GPL-2.0, GPL-3.0, "
     "AGPL-3.0, LGPL-2.1, LGPL-3.0, MPL-2.0. Permissive: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, Unlicense. "
     "Reply '<k> copyleft, <p> permissive, <u> unknown'. If k > 0 also attach a carousel card with one item per "
     "copyleft license (title = license id, subtitle 'copyleft'), each with a button 'Review' whose selection is "
     "{\"action\":\"review\",\"license\":\"<id>\"}.",
     case("mixed", t("MIT, GPL-3.0, Apache-2.0, AGPL-3.0", ACK, has("2 copyleft, 2 permissive, 0 unknown"),
                     card("carousel", min_items=2, contains="AGPL-3.0"))),
     case("clean", t("ISC, BSD-3-Clause, WTFPL", has("0 copyleft, 2 permissive, 1 unknown"), NOCARD)))

spec("promotion", "p05_interest_multi", "Two-agent compound interest advisor",
     "Build TWO agents: a 'calculator' agent that computes compound interest, and an 'advisor' entry agent. The user "
     "writes '<principal> at <rate>% for <years> years' (annual compounding). The advisor asks the calculator agent, "
     "then replies 'Final balance: <amount>' (rounded to 2 decimals) with a review card titled 'Lock in this plan?' "
     "with an Approve button (selection {\"action\":\"approve\"}) and a Reject button (selection {\"action\":\"reject\"}). "
     "On approve reply 'Plan locked'; on reject reply 'Plan discarded'.",
     case("ten_years", t("1000 at 5% for 10 years", ACK, has("Final balance"), num(1628.89), card("review"))),
     case("decimal_rate", t("2500 at 3.5% for 4 years", num(2868.81))),
     case("approve", t(J({"action": "approve"}), has("Plan locked"))),
     case("reject", t(J({"action": "reject"}), has("Plan discarded"))))

spec("promotion", "p06_bases", "Number base converter",
     "Number base converter. 'hex <decimal>' replies '0x<lowercase hex>'. 'dec <hex>' (with or without 0x) replies the "
     "decimal value. 'bin <decimal>' replies '0b<binary>'. Invalid input replies 'error: invalid number'.",
     case("hex", t("hex 255", ACK, has("0xff", ci=False))),
     case("dec", t("dec 0x1F4", num(500, 0))),
     case("bin", t("bin 10", has("0b1010"))),
     case("bad", t("hex banana", has("error: invalid number"))))

spec("promotion", "p07_http_methods", "HTTP method semantics",
     "HTTP method facts (RFC 9110). The user names an HTTP method, possibly in a sentence and in any case; reply "
     "'<METHOD>: safe=<yes|no>, idempotent=<yes|no>'. GET, HEAD, OPTIONS, TRACE are safe and idempotent; PUT and DELETE "
     "are idempotent but not safe; POST, PATCH and CONNECT are neither. Unknown methods: 'Unknown method'.",
     case("put", t("is put idempotent?", ACK, has("PUT: safe=no, idempotent=yes"))),
     case("get", t("GET", has("GET: safe=yes, idempotent=yes"))),
     case("patch", t("patch", has("PATCH: safe=no, idempotent=no"))),
     case("unknown", t("FETCH", has("Unknown method"))))

spec("promotion", "p08_release_review", "Release gate with review card",
     "Release gate agent. When the user writes 'release <version> of <service>', reply 'Release <service> <version>?' "
     "with a review card whose summary rows include Service and Version, an Approve button (selection "
     "{\"action\":\"approve\",\"service\":\"<service>\",\"version\":\"<version>\"}) and a Reject button (selection "
     "{\"action\":\"reject\",\"service\":\"<service>\",\"version\":\"<version>\"}). On approve reply 'Released <service> "
     "<version>'. On reject reply 'Release of <service> <version> blocked'.",
     case("ask", t("release 2.4.0 of checkout", ACK, has("Release checkout 2.4.0?"), card("review"))),
     case("approve", t(J({"action": "approve", "service": "checkout", "version": "2.4.0"}), has("Released checkout 2.4.0"))),
     case("reject", t(J({"action": "reject", "service": "cart", "version": "1.0.1"}), has("Release of cart 1.0.1 blocked"))))

spec("promotion", "p09_order_desk_multi", "Three-agent order desk with memory and review card",
     "Build THREE agents. 'catalog' knows prices: pen 1.50, notebook 4.00, bag 25.00. 'pricing' computes the total: "
     "10% discount if the subtotal is at least 20.00, then 8% tax on the discounted amount, rounded to 2 decimals. "
     "'desk' is the entry agent: for 'order <qty> <product> [and <qty> <product> ...]' it asks catalog for prices and "
     "pricing for the total, remembers the pending order for that user, and replies 'Total: $<total>' with a review "
     "card titled 'Confirm order' with an Approve button (selection {\"action\":\"confirm\"}) and a Reject button "
     "(selection {\"action\":\"cancel\"}). On confirm reply 'Order confirmed: <n> items, $<total>' (n = total "
     "quantity); on cancel reply 'Order cancelled'; with no pending order reply 'No pending order'. For a product "
     "not in the catalog reply 'Unknown product: <name>'.",
     case("small_order", t("order 2 pen and 1 notebook", ACK, has("Total: $7.56"), card("review"))),
     case("discount_confirm", t("order 1 bag", has("Total: $24.30"), card("review")),
          t(J({"action": "confirm"}), has("Order confirmed: 1 items, $24.30"))),
     case("no_pending", t(J({"action": "confirm"}), has("No pending order"))),
     case("unknown", t("order 1 laptop", has("Unknown product: laptop"))))

# ------------------------------------------------------------------ FINAL (untouched until the end)
DIFF_OK = """diff --git a/app/main.py b/app/main.py
--- a/app/main.py
+++ b/app/main.py
@@ -1,2 +1,3 @@
 import os
-x = 1
+x = 2
+y = 3
"""
DIFF_RISKY = """diff --git a/migrations/0042_add_col.sql b/migrations/0042_add_col.sql
--- /dev/null
+++ b/migrations/0042_add_col.sql
@@ -0,0 +1,1 @@
+ALTER TABLE users ADD COLUMN plan TEXT;
diff --git a/.github/workflows/deploy.yml b/.github/workflows/deploy.yml
--- a/.github/workflows/deploy.yml
+++ b/.github/workflows/deploy.yml
@@ -1,1 +1,1 @@
-on: push
+on: workflow_dispatch
"""
DIFF_SECRET = """diff --git a/config/.env b/config/.env
--- a/config/.env
+++ b/config/.env
@@ -1,1 +1,1 @@
-API_KEY=old
+API_KEY=new
"""
spec("final", "f01_pr_gate_multi", "Three-agent pull-request gate",
     "Build THREE agents that gate pull requests. 'diff_parser' receives a unified diff and returns the changed file "
     "paths with added/removed line counts (lines starting with '+'/'-', excluding the '+++'/'---' header lines). "
     "'policy' receives those files and flags risks: any path under 'migrations/' or ending in '.sql' (DATABASE), any "
     "path under '.github/workflows/' (CI), any file named '.env' or whose path contains 'secret' (SECRETS), or more "
     "than 300 added lines in total (LARGE). 'gatekeeper' is the entry agent: the user pastes a diff, the gatekeeper "
     "uses the other two agents and replies 'BLOCK: <comma-separated flags>' if any flag is raised, otherwise "
     "'ALLOW: <n> files, +<added> -<removed>', and always attaches a review card titled 'Merge decision' with an "
     "Approve button (selection {\"action\":\"merge\"}) and a Reject button (selection {\"action\":\"close\"}). On the "
     "merge selection reply 'Merge recorded'; on close reply 'PR closed'.",
     case("allow", t(DIFF_OK, ACK, has("ALLOW: 1 files, +2 -1"), card("review"))),
     case("block", t(DIFF_RISKY, has("BLOCK"), has("DATABASE"), has("CI"), card("review"))),
     case("secrets", t(DIFF_SECRET, has("BLOCK"), has("SECRETS"))),
     case("merge", t(J({"action": "merge"}), has("Merge recorded"))))

spec("final", "f02_upc", "UPC-A checker",
     "UPC-A checker. The user sends a UPC-A code. If it is not exactly 12 digits reply 'INVALID UPC: need 12 digits'. "
     "Otherwise compute 3 x (sum of digits in positions 1,3,5,7,9,11) + (sum of digits in positions 2,4,6,8,10); the "
     "check digit is (10 - total mod 10) mod 10 and must equal digit 12. Reply 'VALID UPC' or 'INVALID UPC'.",
     case("valid", t("036000291452", ACK, has("VALID UPC"), lacks("INVALID"))),
     case("invalid", t("036000291453", has("INVALID UPC"))),
     case("short", t("12345", has("need 12 digits"))))

spec("final", "f03_bytes", "Byte size formatter",
     "Byte size formatter. The user sends a non-negative integer number of bytes. Values below 1024 reply '<n> B'. "
     "Otherwise divide by 1024 repeatedly and use KiB, MiB, GiB or TiB with exactly 1 decimal place, e.g. '1.5 KiB'.",
     case("kib", t("1536", ACK, has("1.5 KiB"))),
     case("bytes", t("512", has("512 B"))),
     case("gib", t("1073741824", has("1.0 GiB"))),
     case("five_gib", t("5368709120", has("5.0 GiB"))))

spec("final", "f04_bookmarks", "Bookmarks with memory and carousel",
     "Bookmark manager with per-user memory. 'save <url> as <name>' stores it and replies 'Saved <name>'. 'bookmarks' "
     "replies '<n> bookmarks' and, if n > 0, attaches a carousel card with one item per bookmark (title = name, "
     "subtitle = url) with an 'Open' button whose selection is {\"action\":\"open\",\"name\":\"<name>\"}; if n = 0 reply "
     "'0 bookmarks' with no card. When the open selection arrives as JSON reply 'Opening <url>'.",
     case("flow", t("save https://fetch.ai as fetch", ACK, has("Saved fetch")),
          t("save https://agentverse.ai as av", has("Saved av")),
          t("bookmarks", has("2 bookmarks"), card("carousel", min_items=2)),
          t(J({"action": "open", "name": "av"}), has("Opening https://agentverse.ai"))),
     case("empty", t("bookmarks", has("0 bookmarks"), NOCARD)))

spec("final", "f05_date_diff", "Date difference",
     "Date difference agent. The user sends 'YYYY-MM-DD to YYYY-MM-DD'; reply '<N> days' where N is the second date "
     "minus the first (can be negative). If either date is not a real date reply 'Invalid date'.",
     case("forward", t("2026-01-01 to 2026-11-07", ACK, has("310 days"))),
     case("negative_leap", t("2024-03-01 to 2024-02-01", has("-29 days"))),
     case("invalid", t("2026-02-30 to 2026-03-01", has("Invalid date"))))

spec("final", "f06_port_form", "Port lookup with form card",
     "Well-known port lookup. Known services: ssh 22, smtp 25, dns 53, http 80, https 443, postgres 5432, redis 6379, "
     "mongodb 27017. If the message names a known service (as a whole word), reply '<service> uses port <port>'. If no "
     "known service is mentioned, reply 'Which service?' with a form card containing a select field named 'service' "
     "whose options are the eight services, and a submit button whose selection is {\"action\":\"lookup\"}. When the "
     "submitted form arrives as JSON (e.g. {\"action\":\"lookup\",\"service\":\"redis\"}) answer the same way.",
     case("postgres", t("what port does postgres use", ACK, has("postgres uses port 5432"))),
     case("https_word", t("https", has("https uses port 443"))),
     case("form", t("which port?", has("Which service"), card("form", fields=["service"]))),
     case("submit", t(J({"action": "lookup", "service": "redis"}), has("redis uses port 6379"))))

spec("final", "f07_release_notes_multi", "Two-agent release notes",
     "Build TWO agents: a 'classifier' agent that classifies conventional-commit messages by type (feat, fix, docs, "
     "chore, refactor, test; the type is the text before the first ':' or '('; anything else is 'other'), and a "
     "'release_notes' entry agent. The user pastes commit messages one per line; release_notes asks the classifier "
     "agent and replies with counts in exactly this order: 'feat=<a> fix=<b> docs=<c> chore=<d> refactor=<e> test=<f> "
     "other=<g>', plus a carousel card with one item per type whose count is > 0 (title = type, subtitle = "
     "'<count> commits').",
     case("mixed", t("feat: add login\nfix(api): null check\nfeat(ui): dark mode\nchore: bump deps\nwip stuff", ACK,
                     has("feat=2 fix=1 docs=0 chore=1 refactor=0 test=0 other=1"), card("carousel", min_items=4))),
     case("single", t("docs: readme", has("feat=0 fix=0 docs=1 chore=0 refactor=0 test=0 other=0"),
                      card("carousel", min_items=1))))

spec("final", "f08_semver_range", "Semver range checker",
     "Semver range checker. Message: 'does <version> satisfy <range>' where range is '^X.Y.Z' or '~X.Y.Z'. Caret: "
     "same major as X and >= X.Y.Z (when X is 0, the minor must also match). Tilde: same major and minor and >= X.Y.Z. "
     "Reply 'YES: <version> <range>' or 'NO: <version> <range>'.",
     case("caret_yes", t("does 1.4.2 satisfy ^1.2.0", ACK, rx(r"^\s*YES"), has("1.4.2 ^1.2.0"))),
     case("caret_major", t("does 2.0.0 satisfy ^1.2.0", rx(r"^\s*NO"))),
     case("caret_zero", t("does 0.3.5 satisfy ^0.2.1", rx(r"^\s*NO"))),
     case("tilde_yes", t("does 1.2.9 satisfy ~1.2.3", rx(r"^\s*YES"))))


spec("final", "f09_incident_router_multi", "Two-agent incident router with memory, carousel and review card",
     "Build TWO agents. 'oncall' holds the schedule: payments -> maria, search -> dev, auth -> kim. 'router' is the "
     "entry agent: for 'incident: <service> <severity>' (severity sev1, sev2 or sev3) it asks oncall who is on call, "
     "records an incident for that user with id INC-<n> (n counts from 1 per user), and replies 'Paged <person> for "
     "<service> (<severity>)'. For sev1 it also attaches a review card titled 'Escalate to manager?' with an Approve "
     "button (selection {\"action\":\"escalate\",\"incident\":\"INC-<n>\"}) and a Reject button (selection "
     "{\"action\":\"dismiss\"}); sev2/sev3 replies have no card. On the escalate selection reply '<incident> "
     "escalated'. 'incidents' replies '<k> open incidents' with a carousel card, one item per incident (title = id, "
     "subtitle = '<service> <severity>'). Unknown service: 'No on-call for <service>'.",
     case("flow", t("incident: payments sev1", ACK, has("Paged maria for payments (sev1)"), card("review")),
          t(J({"action": "escalate", "incident": "INC-1"}), has("INC-1 escalated")),
          t("incident: search sev3", has("Paged dev for search (sev3)"), NOCARD),
          t("incidents", has("2 open incidents"), card("carousel", min_items=2))),
     case("unknown", t("incident: billing sev2", has("No on-call for billing"))))


if __name__ == "__main__":
    dirs = {"dev": ROOT / "bench" / "dev", "promotion": ROOT / "bench" / "sealed" / "promotion",
            "final": ROOT / "bench" / "sealed" / "final"}
    for split, specs in SPECS.items():
        dirs[split].mkdir(parents=True, exist_ok=True)
        for old in dirs[split].glob("*.json"):
            old.unlink()
        for s in specs:
            (dirs[split] / f"{s['id']}.json").write_text(json.dumps(s, indent=1) + "\n")
        print(split, len(specs))
